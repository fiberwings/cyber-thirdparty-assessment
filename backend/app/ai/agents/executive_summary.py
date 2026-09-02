"""Executive summary — one reasoner synthesis pass over the fully scored
assessment.

This is the only place the pipeline asks a model to *prioritise*: given the
aggregate score, every scenario, every weakness and every assessment-quality
signal, produce the verdict, the ranked key risks, the limitations and the
recommended asks that a risk owner reads first.

The output is validated against `ExecutiveSummaryOut`, then post-validated:
scenario codes / weakness ids the model invented are stripped and the removal
is recorded in `limitations` (surfaced, never masked). The persisted blob
carries a fingerprint of the scored state so the report API can flag the
summary as stale after edits/recalcs without silently re-spending a reasoner
call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.ai.context import assessment_context_block
from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, _resolve_model, call_structured
from app.config import settings
from app.models import Assessment
from app.schemas.ai import ExecutiveSummaryOut

SYSTEM_PROMPT = load_prompt("executive_summary")

_LEVEL_NAME = {1: "Low", 2: "Medium", 3: "High", 4: "Very High"}


def compute_fingerprint(a: Assessment) -> str:
    """Hash of the scored state a summary is based on.

    Uses persisted residuals — call after a recalculation. Aggregate band is a
    pure function of the scenarios, so it doesn't need to be included.
    """
    payload = {
        "as_of_date": a.as_of_date,
        "standards_profile": a.standards_profile or {},
        "scenarios": sorted(
            (s.code, s.score_band, s.residual_impact, s.residual_likelihood)
            for s in a.scenarios
        ),
        "weaknesses": sorted(
            (w.id, w.severity, bool(w.unmatched)) for w in a.weaknesses
        ),
        "meta_issues": sorted(m.id for m in a.meta_issues),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _format_scenarios(a: Assessment, scenario_reads) -> str:
    parts = []
    for sr in scenario_reads:
        parts.append(
            f"[code={sr.code}] {sr.name}\n"
            f"  inherent: impact={_LEVEL_NAME[sr.inherent_impact]}, "
            f"likelihood={_LEVEL_NAME[sr.inherent_likelihood]}\n"
            f"  residual: impact={_LEVEL_NAME[sr.residual_impact]}, "
            f"likelihood={_LEVEL_NAME[sr.residual_likelihood]}, band={sr.band}\n"
            f"  control coverage index: {sr.coverage_index:.2f} (0=no controls evidenced, 1=fully evidenced)\n"
            f"  uplift applied: +{sr.uplift} band(s) "
            f"({sr.distinct_high_critical} distinct high/critical deficiencies, "
            f"{sr.auditor_tested_high_critical} auditor-tested)\n"
            f"  evidence confidence: {sr.confidence} (assessment-quality label — qualify wording accordingly)\n"
            f"  rationale: {sr.rationale or '(none written)'}"
        )
    return "\n\n".join(parts) if parts else "(no scenarios)"


def _format_weaknesses(a: Assessment) -> str:
    parts = []
    doc_names = {d.id: d.filename for d in a.documents}
    for w in sorted(a.weaknesses, key=lambda w: w.id):
        doc = w.document.filename if w.document is not None else "unknown document"
        scored = "unscored (unmatched)" if w.unmatched else (
            f"scored via {', '.join(w.mapped_control_codes or []) or 'no codes'}"
        )
        kind = f" kind={w.kind_signal}" if w.kind_signal else ""
        entry = (
            f"[id={w.id} severity={w.severity}{kind} source={doc}] {scored}\n"
            f"  {w.description}"
        )
        if len(w.evidence_refs or []) > 1:
            # Contradictions: show every side so the summary can cite both.
            for ref in w.evidence_refs:
                rdoc = doc_names.get(ref.get("document_id"), f"document {ref.get('document_id')}")
                entry += f'\n  - {rdoc}: "{ref.get("quote", "")}"'
        parts.append(entry)
    return "\n\n".join(parts) if parts else "(no weaknesses extracted)"


def _format_meta_issues(a: Assessment) -> str:
    parts = [
        f"- kind={m.kind} target={m.target_ref}: {m.rationale or '(no rationale)'}"
        for m in a.meta_issues
    ]
    # Controls with citations that could not be located in their documents are
    # an evidence-quality signal the model must fold into limitations.
    unresolved = []
    for s in a.scenarios:
        for ec in s.expected_controls:
            ca = ec.assessment
            if ca is not None and ca.unresolved_citations:
                unresolved.append(
                    f"- unresolved citations: {len(ca.unresolved_citations)} quote(s) for "
                    f"control {s.code}/{ec.code} could not be located in the cited document"
                )
    return "\n".join(parts + unresolved) or "(none)"


def _format_documents(a: Assessment) -> str:
    parts = [
        f"- {d.filename} (kind={d.kind}, uploaded="
        f"{d.created_at.strftime('%Y-%m-%d') if d.created_at else 'unknown'})"
        for d in a.documents
    ]
    return "\n".join(parts) or "(no documents uploaded)"


def _build_messages(a: Assessment, scenario_reads, aggregate) -> list[dict]:
    user = (
        f"{assessment_context_block(a)}\n\n"
        f"# Vendor\n{a.vendor_name}\n\n"
        f"# Overall residual score\nband={aggregate.band} "
        f"(top-2 mean rank {aggregate.top2_mean_rank}, "
        f"impact-weighted mean rank {aggregate.weighted_mean_rank}; "
        "overall = the more conservative of the two)\n\n"
        f"# Scenarios\n{_format_scenarios(a, scenario_reads)}\n\n"
        f"# Weaknesses\n{_format_weaknesses(a)}\n\n"
        f"# Meta-issues (assessment-quality signals)\n{_format_meta_issues(a)}\n\n"
        f"# Documents reviewed\n{_format_documents(a)}\n\n"
        "Write the executive summary. Output JSON per schema."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _strip_unknown_references(
    out: ExecutiveSummaryOut, a: Assessment
) -> ExecutiveSummaryOut:
    """Remove references the model invented; record each removal as a limitation."""
    valid_codes = {s.code for s in a.scenarios}
    valid_wids = {w.id for w in a.weaknesses}
    removed: list[str] = []

    for risk in out.key_risks:
        bad_codes = [c for c in risk.scenario_codes if c not in valid_codes]
        bad_wids = [i for i in risk.weakness_ids if i not in valid_wids]
        if bad_codes:
            risk.scenario_codes = [c for c in risk.scenario_codes if c in valid_codes]
            removed.append(
                f"Summary referenced unknown scenario code(s) {', '.join(bad_codes)} "
                f"in key risk '{risk.title}' — reference removed."
            )
        if bad_wids:
            risk.weakness_ids = [i for i in risk.weakness_ids if i in valid_wids]
            removed.append(
                f"Summary referenced unknown weakness id(s) "
                f"{', '.join(str(i) for i in bad_wids)} in key risk '{risk.title}' — "
                "reference removed."
            )
    for action in out.recommended_actions:
        bad_codes = [c for c in action.related_scenario_codes if c not in valid_codes]
        if bad_codes:
            action.related_scenario_codes = [
                c for c in action.related_scenario_codes if c in valid_codes
            ]
            removed.append(
                f"Summary referenced unknown scenario code(s) {', '.join(bad_codes)} "
                f"in recommended action '{action.action[:60]}' — reference removed."
            )

    out.limitations = list(out.limitations) + removed
    return out


async def write(
    db: Session,
    assessment_id: int,
    *,
    client: OpenRouterClient | None = None,
) -> ExecutiveSummaryOut:
    """Generate and persist the executive summary for a scored assessment."""
    from app.api.scoring import _recalculate_in_session  # lazy: avoid API↔agent cycle

    a = db.get(Assessment, assessment_id)
    if a is None:
        raise ValueError(f"Assessment {assessment_id} not found")

    scenario_reads, aggregate = _recalculate_in_session(db, a)

    model_override = (a.model_overrides or {}).get("executive_summary")
    out: ExecutiveSummaryOut = await call_structured(
        db,
        purpose="executive_summary",
        profile="reasoner",
        messages=_build_messages(a, scenario_reads, aggregate),
        schema=ExecutiveSummaryOut,
        assessment_id=a.id,
        model_override=model_override,
        max_tokens=settings.llm_budget_large,
        client=client,
    )
    out = _strip_unknown_references(out, a)

    a.executive_summary = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "model_id": _resolve_model("reasoner", model_override),
        "fingerprint": compute_fingerprint(a),
        "summary": out.model_dump(),
    }
    db.commit()
    return out
