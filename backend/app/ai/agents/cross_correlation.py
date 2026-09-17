"""Cross-correlation: map extracted weaknesses to existing controls or spawn
emergent scenarios.

Replaces the legacy `weaknesses.py` synthesizer. Where the old agent did one
heuristic-driven scan over a 60-chunk pool, this one operates on the
already-extracted `Weakness` rows produced by `document_weaknesses` and decides
how each one reaches the score:

    1. Try mapping to one or more existing scenario expected_control codes.
       When mapped, `Weakness.mapped_control_codes` is set and `unmatched` is
       cleared so the scoring engine can apply per-scenario weakness uplift.

    2. When a cluster doesn't fit any existing scenario's controls, the
       reasoner may propose a brand-new emergent scenario with its own
       expected_controls. Persisted as `Scenario(source="emergent_from_weakness")`
       with `origin_weakness_ids` set to the weaknesses that justify it.

    3. Anything still unmapped at high/critical severity after all
       reasoner-driven clusters runs through a deterministic floor:
       per-kind_signal forced emergent scenarios are created so high-severity
       findings always reach the residual band even when the reasoner
       refuses to spawn an emergent scenario.

The agent is idempotent: calling it twice in a row makes no further changes
(weaknesses already mapped stay mapped; forced emergents are dedup'd by
existing scenario.code).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
import json

from sqlalchemy.orm import Session

from app import activity
from app.ai.context import analysis_datetime, standards_block
from app.ai.prompts import load as load_prompt
from app.ai.router import LLMClient, LLMError, call_structured
from app.config import settings
from app.db import SessionLocal
from app.models import Assessment, ExpectedControl, MetaIssue, Scenario, Weakness
from app.scoring.engine import band_for
from app.schemas.ai import (
    CrossCorrelationOut,
    WeaknessClusterMappingOut,
)

ProgressCb = Callable[[float, str], Awaitable[None]]

SYSTEM_PROMPT = load_prompt("cross_correlation")
_CATALOG_PATH = Path(__file__).parent.parent / "catalog" / "controls.json"

_BATCH_SIZE = 8  # max weaknesses per cluster reasoner call
_PHASE_CONCURRENCY = 4

_SEVERITY_TO_LEVEL = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _catalog_lines() -> str:
    data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return "\n".join(f"- {c['code']} — {c['name']} ({c['family']})" for c in data["controls"])


def _format_existing_scenarios(scenarios: list[Scenario]) -> str:
    if not scenarios:
        return "(no existing scenarios)"
    parts: list[str] = []
    for s in scenarios:
        ctrls = "\n".join(
            f"    - {ec.code} — {ec.name}" for ec in s.expected_controls
        ) or "    (no expected controls)"
        parts.append(
            f"[code: {s.code}] {s.name} (source={s.source})\n"
            f"  {s.description}\n"
            f"{ctrls}"
        )
    return "\n\n".join(parts)


def _format_source_doc(w: Weakness, analysis_dt: datetime) -> str:
    doc = w.document
    if doc is None:
        return ""
    if doc.created_at is None:
        return f' [source_doc="{doc.filename}" uploaded=unknown]'
    days_ago = max(0, (analysis_dt - doc.created_at).days)
    return (
        f' [source_doc="{doc.filename}" '
        f"uploaded={doc.created_at.strftime('%Y-%m-%d')}, {days_ago} days ago]"
    )


def _format_weaknesses(batch: list[Weakness], analysis_dt: datetime) -> str:
    parts = []
    for w in batch:
        hint = ""
        if w.mapped_control_codes:
            hint = f" (suggested: {', '.join(w.mapped_control_codes)})"
        parts.append(
            f"[id={w.id} severity={w.severity} kind_signal={w.kind_signal}]"
            f"{hint}{_format_source_doc(w, analysis_dt)}\n"
            f"description: {w.description}\n"
            f"quote: {w.quote or '(no quote)'}"
        )
    return "\n\n".join(parts)


def _build_messages(
    scenarios: list[Scenario],
    batch: list[Weakness],
    analysis_dt: datetime,
    standards: str = "",
) -> list[dict]:
    user = (
        f"# Analysis date: {analysis_dt.strftime('%Y-%m-%d')}\n"
        f"{standards}\n"
        "# Existing scenarios with their expected controls\n"
        f"{_format_existing_scenarios(scenarios)}\n\n"
        "# Control catalogue (additional codes you may use when proposing emergent scenarios)\n"
        f"{_catalog_lines()}\n\n"
        "# Weaknesses to map\n"
        f"{_format_weaknesses(batch, analysis_dt)}\n\n"
        "Return one cluster mapping per the schema. Use only the integer "
        "weakness_ids shown above. Use only control codes that appear in the "
        "scenarios above or in the catalogue (or X- prefixed for invented codes "
        "in propose_emergent only)."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def _make_unique_code(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


def _apply_mapping(
    inner: Session,
    cluster: WeaknessClusterMappingOut,
    valid_codes: set[str],
    existing_scenario_codes: set[str],
    assessment_id: int,
) -> tuple[int, int, int]:
    """Apply one cluster's mappings.

    Returns (mapped_count, emergent_count, unscored_count).
    """
    mapped_count = 0
    unscored_count = 0
    for m in cluster.weakness_mappings:
        w = inner.get(Weakness, m.weakness_id)
        if w is None or w.assessment_id != assessment_id:
            continue
        # Restrict to codes the model was authorised to use.
        good_codes = [c for c in m.mapped_control_codes if c in valid_codes]
        if good_codes:
            w.mapped_control_codes = sorted(set(good_codes))
            w.unmatched = False
            mapped_count += 1
        elif m.mapped_control_codes:
            # The model matched this weakness to controls that exist only in
            # the catalogue, not on any scenario — it cannot reach the score
            # through a mapping. Keep it unmatched (high/critical will still
            # hit the forced-emergent floor) but preserve the proposal as a
            # suggestion and surface an explicit, non-scoring audit record so
            # the finding is never silently lost.
            proposed = sorted(set(m.mapped_control_codes))
            w.mapped_control_codes = proposed
            target_ref = f"weakness:{w.id}"
            already = (
                inner.query(MetaIssue)
                .filter(
                    MetaIssue.assessment_id == assessment_id,
                    MetaIssue.kind == "unscored_finding",
                    MetaIssue.target_ref == target_ref,
                )
                .first()
            )
            if already is None:
                inner.add(
                    MetaIssue(
                        assessment_id=assessment_id,
                        kind="unscored_finding",
                        scenario_code=None,
                        target_ref=target_ref,
                        weight=0.0,
                        rationale=(
                            f"{w.severity} weakness maps to catalogue control(s) "
                            f"{', '.join(proposed)} that are not expected controls of "
                            "any scenario; it does not contribute to the residual "
                            "score. Review whether a scenario should cover it."
                        ),
                    )
                )
            unscored_count += 1
        # else: no codes proposed at all; leave unmatched for propose_emergent
        # or the forced-emergent floor.

    emergent_count = 0
    if cluster.propose_emergent is not None:
        s_out = cluster.propose_emergent
        code = _make_unique_code(s_out.code, existing_scenario_codes)
        existing_scenario_codes.add(code)
        # The model may include codes outside the catalogue for invented
        # X-prefixed controls; the schema doesn't restrict that. We keep
        # them as-is — `expected_controls` are scenario-local labels.
        s = Scenario(
            assessment_id=assessment_id,
            code=code,
            name=s_out.name,
            description=s_out.description,
            source="emergent_from_weakness",
            inherent_impact=s_out.inherent_impact,
            inherent_likelihood=s_out.inherent_likelihood,
            residual_impact=s_out.inherent_impact,
            residual_likelihood=s_out.inherent_likelihood,
            # Inherent band from the 4x4 matrix; recalculate overwrites it
            # with the residual band once gap analysis has run.
            score_band=band_for(s_out.inherent_impact, s_out.inherent_likelihood),
            origin_weakness_ids=list(cluster.origin_weakness_ids),
        )
        inner.add(s)
        inner.flush()
        for c_out in s_out.expected_controls:
            inner.add(
                ExpectedControl(
                    scenario_id=s.id,
                    code=c_out.code,
                    name=c_out.name,
                    description=c_out.description,
                    weight=c_out.weight,
                    rationale=c_out.rationale,
                )
            )
        # Mark origin weaknesses as no longer unmatched (they're now covered
        # by the new emergent scenario's expected controls).
        for wid in cluster.origin_weakness_ids:
            w = inner.get(Weakness, wid)
            if w is not None and w.assessment_id == assessment_id:
                w.unmatched = False
        emergent_count = 1
    return mapped_count, emergent_count, unscored_count


def _force_emergent_for_unmapped(
    inner: Session,
    assessment_id: int,
    existing_codes: set[str],
) -> int:
    """Deterministic floor: any high/critical weakness still flagged unmatched
    after the reasoner pass spawns a forced emergent scenario per kind_signal.
    Guarantees high-severity findings reach the score under the accuracy
    directive.
    """
    rows: list[Weakness] = (
        inner.query(Weakness)
        .filter(
            Weakness.assessment_id == assessment_id,
            Weakness.unmatched.is_(True),
            Weakness.status == "confirmed",
            Weakness.severity.in_(["high", "critical"]),
        )
        .all()
    )
    if not rows:
        return 0
    by_kind: dict[str, list[Weakness]] = defaultdict(list)
    for w in rows:
        by_kind[w.kind_signal or "other"].append(w)

    created = 0
    for kind_signal, weaknesses in by_kind.items():
        max_sev = max(_SEVERITY_TO_LEVEL.get(w.severity, 1) for w in weaknesses)
        readable_kind = (kind_signal or "other").replace("_", " ")
        base = f"WEAK_{kind_signal.upper()}"
        code = _make_unique_code(base, existing_codes)
        existing_codes.add(code)
        s = Scenario(
            assessment_id=assessment_id,
            code=code,
            name=f"Unmitigated {readable_kind} cluster",
            description=(
                f"Cluster of {len(weaknesses)} high/critical {readable_kind}(s) "
                "identified in vendor evidence that did not map onto any existing scenario's "
                "expected controls. Auto-spawned so these findings reach the residual "
                "score; merge into a more specific scenario or refine the controls if "
                "appropriate."
            ),
            source="emergent_from_weakness",
            inherent_impact=max_sev,
            inherent_likelihood=3,
            residual_impact=max_sev,
            residual_likelihood=3,
            # Matrix lookup replaces the old ad-hoc heuristic ("High" if
            # max_sev >= 3): identical for sev 3, and correctly VeryHigh for
            # sev 4 instead of understating it.
            score_band=band_for(max_sev, 3),
            origin_weakness_ids=[w.id for w in weaknesses],
        )
        inner.add(s)
        inner.flush()
        ec_code = f"X-REMEDIATE-{kind_signal.upper()}"
        inner.add(
            ExpectedControl(
                scenario_id=s.id,
                code=ec_code,
                name=f"Remediation programme for {readable_kind} findings",
                description=(
                    "Tracked, time-bound remediation of every underlying finding "
                    "with documented evidence on closure."
                ),
                weight=1.5,
                rationale=(
                    f"Required because {len(weaknesses)} high/critical "
                    f"{readable_kind}(s) were detected without coverage by any "
                    "existing scenario's expected controls."
                ),
            )
        )
        for w in weaknesses:
            w.unmatched = False
            # Leave a mapping pointer so the scoring engine can apply uplift.
            w.mapped_control_codes = sorted(
                set(list(w.mapped_control_codes or []) + [ec_code])
            )
        created += 1
    return created


async def run(
    db: Session,
    assessment_id: int,
    *,
    on_progress: ProgressCb | None = None,
    client: LLMClient | None = None,
) -> dict[str, int]:
    """Map every unmatched Weakness to existing controls or emergent scenarios.

    Returns a small stats dict: {mapped, emergent_proposed, forced_emergent}.
    """
    a = db.get(Assessment, assessment_id)
    if a is None:
        raise ValueError(f"Assessment {assessment_id} not found")

    # Phase 4: deterministic attestation checks first (they need only the
    # stored profiles + analysis date + standards; re-runs replace).
    from app.ai.agents import attestation as attestation_agent

    activity.stage("Attestation checks")
    attestation_agent.apply_checks(db, assessment_id)

    # R3/R4: review extracted candidates against the whole bundle first, then
    # consolidate; only confirmed rows are correlated and scored.
    from app.ai.agents import confirmation as confirm_agent

    async def review_progress(p: float, detail: str):
        if on_progress:
            await on_progress(0.3 * p, f"Confirming candidates — {detail}")

    review_counts = await confirm_agent.review(
        db, assessment_id, client=client, on_progress=review_progress
    )
    a = db.get(Assessment, assessment_id)
    scenarios = list(a.scenarios)
    weaknesses = [w for w in a.weaknesses if w.unmatched]
    if not weaknesses:
        if on_progress:
            await on_progress(1.0, "No unmatched weaknesses; nothing to correlate.")
        return {"mapped": 0, "emergent_proposed": 0, "forced_emergent": 0, "unscored": 0, **{f"review_{k}": v for k, v in review_counts.items()}}

    # Valid control codes the reasoner may map to: codes from existing
    # scenarios. The catalogue is for emergent scenario *expected_controls*
    # only — the mapping target must be a code that already drives scoring.
    valid_codes = {ec.code for s in scenarios for ec in s.expected_controls}
    existing_scenario_codes = {s.code for s in scenarios}
    model_override = (a.model_overrides or {}).get("weaknesses")

    # Cluster: bucket by kind_signal, then chunk into batches of _BATCH_SIZE.
    by_kind: dict[str, list[Weakness]] = defaultdict(list)
    for w in weaknesses:
        by_kind[w.kind_signal or "other"].append(w)
    batches: list[list[Weakness]] = []
    for kind, ws in by_kind.items():
        for i in range(0, len(ws), _BATCH_SIZE):
            batches.append(ws[i : i + _BATCH_SIZE])

    # Single analysis date shared across all cluster calls so the model sees
    # a consistent "now" when reasoning about source-document age. It is the
    # assessment's as_of_date (R7), never the wall clock.
    analysis_dt = analysis_datetime(a)
    standards = standards_block(a)

    semaphore = asyncio.Semaphore(_PHASE_CONCURRENCY)
    n = len(batches)
    done = 0
    activity.stage("Correlating clusters", units_total=n, unit_label="clusters")
    mapped_total = 0
    emergent_total = 0
    unscored_total = 0

    async def correlate(inner: Session, batch: list[Weakness]) -> WeaknessClusterMappingOut:
        return await call_structured(
            inner,
            purpose="cross_correlation",
            profile="reasoner",
            messages=_build_messages(scenarios, batch, analysis_dt, standards),
            schema=WeaknessClusterMappingOut,
            assessment_id=assessment_id,
            model_override=model_override,
            max_tokens=settings.llm_budget_large,
            client=client,
        )

    async def worker(batch: list[Weakness]) -> list[WeaknessClusterMappingOut]:
        nonlocal done
        async with semaphore:
            with SessionLocal() as inner:
                try:
                    outs = [await correlate(inner, batch)]
                except LLMError as e:
                    if not e.truncated or len(batch) < 2:
                        raise
                    # Even the router's enlarged-budget retry truncated: split
                    # the cluster in half and correlate each part on its own
                    # (batches are ≤ _BATCH_SIZE, so one split is enough).
                    if on_progress:
                        await on_progress(
                            0.05 + 0.85 * done / n,
                            f"Cluster of {len(batch)} truncated; splitting in half",
                        )
                    mid = len(batch) // 2
                    outs = [
                        await correlate(inner, batch[:mid]),
                        await correlate(inner, batch[mid:]),
                    ]
        done += 1
        activity.advance(done)
        if on_progress:
            await on_progress(
                0.05 + 0.85 * done / n,
                f"Correlated cluster {done}/{n}",
            )
        return outs

    results = await asyncio.gather(
        *[worker(b) for b in batches], return_exceptions=True
    )

    failures: list[BaseException] = []
    # Apply all successful clusters in a single session, then commit.
    with SessionLocal() as inner:
        for result in results:
            if isinstance(result, BaseException):
                failures.append(result)
                continue
            for out in result:
                mapped, emergent, unscored = _apply_mapping(
                    inner, out, valid_codes, existing_scenario_codes, assessment_id
                )
                mapped_total += mapped
                emergent_total += emergent
                unscored_total += unscored
        inner.commit()

        # Deterministic floor for any high/critical still unmatched.
        activity.stage("Accuracy floor")
        if on_progress:
            await on_progress(0.92, "Applying high/critical accuracy floor...")
        forced = _force_emergent_for_unmapped(
            inner, assessment_id, existing_scenario_codes
        )
        inner.commit()

    if failures:
        first = failures[0]
        msg = (
            f"cross_correlation: {len(failures)}/{n} clusters failed; "
            f"first failure: {first}"
        )
        if isinstance(first, LLMError):
            raise LLMError(
                msg,
                transient=first.transient,
                upstream_code=first.upstream_code,
                truncated=first.truncated,
            )
        raise RuntimeError(msg)

    if on_progress:
        await on_progress(
            1.0,
            f"Mapped {mapped_total}; proposed {emergent_total} emergent; "
            f"forced {forced}; {unscored_total} unscored (catalogue-only)",
        )
    return {
        "mapped": mapped_total,
        "emergent_proposed": emergent_total,
        "forced_emergent": forced,
        "unscored": unscored_total,
    }
