"""Bundle-aware confirmation of extracted candidates (R3) and thin merge (R4).

Extraction sees one document at a time and produces *candidates*. Before
anything is scored or correlated, every candidate is reviewed once against
the WHOLE evidence bundle (one reasoner call per source document) and becomes
confirmed / evidence_note / dropped, with the reason persisted on the row.
Confirmed rows are then consolidated: one row per deficiency, every quote
kept as an evidence ref, the merged rows flagged (never deleted).

Both steps are judgement calls made by the model with the bundle in context
— there is no rule list. Failure handling protects recall: a candidate the
model does not decide on is confirmed and flagged, never silently lost.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy.orm import Session

from app.ai.agents.gap_analysis import format_bundle, load_bundle
from app.ai.context import assessment_context_block
from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, call_structured
from app.models import Assessment, Weakness
from app.schemas.ai import CandidateReviewOut, MergeOut

ProgressCb = Callable[[float, str], Awaitable[None]]

CONFIRM_PROMPT = load_prompt("weakness_confirmation")
MERGE_PROMPT = load_prompt("weakness_merge")
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _fmt_candidate(w: Weakness) -> str:
    doc = w.document.filename if w.document is not None else "unknown"
    return (
        f"- id={w.id} severity={w.severity} kind_signal={w.kind_signal} source=\"{doc}\" "
        f"chunk_id={w.source_chunk_id}\n  description: {w.description}\n  quote: \"{w.quote or ''}\""
    )


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


async def confirm_candidates(
    db: Session,
    assessment: Assessment,
    *,
    bundle_text: str,
    client: OpenRouterClient | None = None,
    on_progress: ProgressCb | None = None,
) -> dict[str, int]:
    """Review every candidate (status="candidate", not user-edited) against
    the bundle, one call per source document. Returns counts by decision."""
    model_override = (assessment.model_overrides or {}).get("weaknesses")
    candidates = [
        w for w in assessment.all_weaknesses
        if w.status == "candidate" and not w.user_edited
    ]
    counts = {"confirmed": 0, "evidence_note": 0, "dropped": 0, "unreviewed_kept": 0}
    if not candidates:
        return counts
    by_doc: dict[int | None, list[Weakness]] = {}
    for w in candidates:
        by_doc.setdefault(w.source_document_id, []).append(w)

    from app.ai.router import OpenRouterError

    async def decide(doc_id: int | None, doc_name: str, rows: list[Weakness]) -> list:
        """One review call for these candidates. Output truncation splits the
        batch in half (recursively); a single candidate that still cannot be
        reviewed is kept-and-flagged by the caller (never fatal, never lost)."""
        user = (
            f"{assessment_context_block(assessment)}\n\n"
            f"# Vendor\n{assessment.vendor_name}\n\n"
            f"{bundle_text}\n\n"
            f"# Candidate weaknesses extracted from \"{doc_name}\" (document_id={doc_id}) — {len(rows)} to review\n"
            + "\n".join(_fmt_candidate(w) for w in rows)
            + "\n\nDecide every candidate above. One short sentence per reason; "
            "no analysis outside the JSON. JSON only."
        )
        try:
            out: CandidateReviewOut = await call_structured(
                db,
                purpose="weakness_confirmation",
                profile="reasoner",
                messages=[{"role": "system", "content": CONFIRM_PROMPT}, {"role": "user", "content": user}],
                schema=CandidateReviewOut,
                assessment_id=assessment.id,
                model_override=model_override,
                max_tokens=8192,
                client=client,
            )
        except OpenRouterError as e:
            if not e.truncated or len(rows) < 2:
                raise
            mid = len(rows) // 2
            return (await decide(doc_id, doc_name, rows[:mid])) + (await decide(doc_id, doc_name, rows[mid:]))
        return list(out.decisions)

    n_docs = len(by_doc)
    for i, (doc_id, rows) in enumerate(by_doc.items()):
        doc_name = rows[0].document.filename if rows[0].document is not None else f"document {doc_id}"
        keep_reason = "not decided by the confirmation model (omitted from its answer); kept for human review"
        try:
            decisions = await decide(doc_id, doc_name, rows)
        except Exception as e:
            decisions = []
            keep_reason = f"confirmation call failed ({str(e)[:120]}); kept for human review"
        wanted = {w.id: w for w in rows}
        decided: set[int] = set()
        for d in decisions:
            w = wanted.get(d.id)
            if w is None or d.id in decided:
                continue
            decided.add(d.id)
            w.status = d.decision
            if d.severity and d.decision == "confirmed":
                w.severity = d.severity  # by consequence, judged with the bundle
            w.review = {
                "decision": d.decision,
                "confidence": d.confidence,
                "reason": d.reason,
                "at": _now(),
                "stage": "confirmation",
            }
            if d.evidence_strength:
                w.review["evidence_strength"] = d.evidence_strength
            counts[d.decision] += 1
        for wid, w in wanted.items():
            if wid in decided:
                continue
            # Never lose a candidate silently: keep it, flag it for the reviewer.
            w.status = "confirmed"
            w.review = {
                "decision": "confirmed",
                "confidence": "low",
                "reason": keep_reason,
                "at": _now(),
                "stage": "confirmation",
                "unreviewed": True,
            }
            counts["unreviewed_kept"] += 1
        db.commit()
        if on_progress:
            await on_progress((i + 1) / n_docs, f"Reviewed {doc_name}: {len(rows)} candidates")
    return counts


def _ref(w: Weakness) -> dict:
    return {
        "document_id": w.source_document_id,
        "chunk_id": w.source_chunk_id,
        "page": w.chunk.page if w.chunk is not None else None,
        "section_path": w.chunk.section_path if w.chunk is not None else "",
        "quote": w.quote or "",
    }


async def merge_confirmed(
    db: Session,
    assessment: Assessment,
    *,
    client: OpenRouterClient | None = None,
) -> int:
    """Thin dedupe: one call over the confirmed document-origin rows; groups
    become one primary row carrying every member's quote as an evidence ref.
    Members are flagged status="merged" (kept for audit). Returns rows merged."""
    model_override = (assessment.model_overrides or {}).get("weaknesses")
    rows = [
        w for w in assessment.all_weaknesses
        if w.status == "confirmed" and w.origin == "document" and not w.user_edited
    ]
    if len(rows) < 2:
        return 0
    by_id = {w.id: w for w in rows}
    user = (
        f"# Vendor\n{assessment.vendor_name}\n\n"
        f"# Confirmed weaknesses ({len(rows)})\n"
        + "\n".join(
            _fmt_candidate(w) + f"\n  control_codes: {', '.join(w.mapped_control_codes or []) or '(none)'}"
            for w in rows
        )
        + "\n\nGroup only rows that are the same underlying deficiency. JSON only."
    )
    out: MergeOut = await call_structured(
        db,
        purpose="weakness_merge",
        profile="reasoner",
        messages=[{"role": "system", "content": MERGE_PROMPT}, {"role": "user", "content": user}],
        schema=MergeOut,
        assessment_id=assessment.id,
        model_override=model_override,
        max_tokens=8192,
        client=client,
    )
    used: set[int] = set()
    merged = 0
    for g in out.groups:
        primary = by_id.get(g.primary_id)
        members = [by_id[m] for m in g.member_ids if m in by_id and m != g.primary_id]
        if primary is None or not members or primary.id in used or any(m.id in used for m in members):
            continue
        used.add(primary.id)
        refs = list(primary.evidence_refs or []) or [_ref(primary)]
        codes = set(primary.mapped_control_codes or [])
        sev = primary.severity
        for m in members:
            used.add(m.id)
            for r in (m.evidence_refs or []) or [_ref(m)]:
                if r not in refs:
                    refs.append(r)
            codes |= set(m.mapped_control_codes or [])
            if _SEVERITY_RANK.get(m.severity, 0) > _SEVERITY_RANK.get(sev, 0):
                sev = m.severity
            m.status = "merged"
            m.review = {
                **(m.review or {}),
                "merged_into": primary.id,
                "merge_reason": g.reason,
                "merged_at": _now(),
                "stage": "merge",
            }
            merged += 1
        primary.evidence_refs = refs
        primary.mapped_control_codes = sorted(codes)
        primary.severity = sev
        primary.description = g.description
        primary.review = {
            **(primary.review or {}),
            "members": [m.id for m in members],
            "merge_reason": g.reason,
            "merged_at": _now(),
        }
    db.commit()
    return merged


async def review(
    db: Session,
    assessment_id: int,
    *,
    client: OpenRouterClient | None = None,
    on_progress: ProgressCb | None = None,
) -> dict[str, int]:
    """Confirmation then merge. Returns counts."""
    a = db.get(Assessment, assessment_id)
    if a is None:
        raise ValueError(f"Assessment {assessment_id} not found")
    docs, chunks = load_bundle(db, assessment_id)
    bundle_text = format_bundle(docs, chunks)
    counts = await confirm_candidates(
        db, a, bundle_text=bundle_text, client=client, on_progress=on_progress
    )
    db.expire_all()
    a = db.get(Assessment, assessment_id)
    counts["merged"] = await merge_confirmed(db, a, client=client)
    db.expire_all()
    return counts
