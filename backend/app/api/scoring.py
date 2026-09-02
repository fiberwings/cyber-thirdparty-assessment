from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import workflow
from app.api.deps import db_session, get_assessment
from app.db import SessionLocal
from app.models import Assessment, Scenario
from app.schemas.api import (
    AggregateScoreRead,
    ScenarioScoreRead,
    TaskStatusRead,
)
from app.scoring.engine import (
    ControlInput,
    MetaIssueInput,
    ScenarioInput,
    WeaknessInput,
    aggregate,
    score_scenario,
)
from app.tasks import mark_phase_done, mark_phase_error, mark_phase_started, registry

router = APIRouter(prefix="/api/assessments", tags=["scoring"])


# kind_signal → evidence strength fallback, used when the confirmation pass
# has not stored one on the row (legacy assessments, gap contradictions).
_STRENGTH_BY_KIND = {
    "soc_exception": "auditor_tested",
    "pentest_finding": "auditor_tested",
    "iso_nonconformity": "auditor_tested",
    "attestation_check": "auditor_tested",   # arithmetic over audited, quoted dates
    "questionnaire_negative": "vendor_admitted",
    "cross_doc_conflict": "vendor_admitted",  # the vendor's own statements disagree
    "policy_gap": "inferred_absence",
    "dpa_clause_missing": "inferred_absence",
}


def _evidence_strength(w) -> str:
    stored = (w.review or {}).get("evidence_strength") if w.review else None
    if stored in ("auditor_tested", "vendor_admitted", "inferred_absence"):
        return stored
    return _STRENGTH_BY_KIND.get(w.kind_signal or "", "vendor_admitted")


def _recalculate_in_session(db: Session, a: Assessment) -> tuple[list[ScenarioScoreRead], AggregateScoreRead]:
    scored = []
    inherent_impacts = {}

    # Pre-bucket every mapped weakness by the control codes it touches so each
    # scenario can pull only the weaknesses that hit one of its expected
    # controls. Unmatched weaknesses don't count here — they're either folded
    # into emergent scenarios by cross_correlation or surfaced as advisory
    # findings, but they don't move a mapped scenario's residual.
    weaknesses_by_code: dict[str, list[tuple[int, str, list[str], str]]] = {}
    for w in a.weaknesses:
        if w.unmatched:
            continue
        codes = list(w.mapped_control_codes or [])
        strength = _evidence_strength(w)
        for code in codes:
            weaknesses_by_code.setdefault(code, []).append((w.id, w.severity, codes, strength))

    for s in a.scenarios:
        ec_codes = [ec.code for ec in s.expected_controls]
        # Deduplicate weaknesses across overlapping mapped codes so one
        # weakness mapped to multiple of this scenario's controls only counts
        # once toward its uplift.
        seen: set[int] = set()
        scenario_weaknesses: list[WeaknessInput] = []
        for code in ec_codes:
            for w_id, sev, w_codes, strength in weaknesses_by_code.get(code, []):
                if w_id in seen:
                    continue
                seen.add(w_id)
                scenario_weaknesses.append(
                    WeaknessInput(
                        severity=sev,
                        mapped_control_codes=list(w_codes),
                        evidence_strength=strength,
                    )
                )

        controls = [
            ControlInput(
                code=ec.code,
                name=ec.name,
                weight=ec.weight,
                coverage=(ec.assessment.coverage if ec.assessment else "none"),
                effectiveness=(ec.assessment.effectiveness if ec.assessment else "unknown"),
            )
            for ec in s.expected_controls
        ]
        meta_issues = [
            MetaIssueInput(kind=m.kind)
            for m in a.meta_issues
            if m.scenario_code == s.code
        ]
        si = ScenarioInput(
            code=s.code,
            inherent_impact=s.inherent_impact,
            inherent_likelihood=s.inherent_likelihood,
            controls=controls,
            meta_issues=meta_issues,
            weaknesses=scenario_weaknesses,
        )
        result = score_scenario(si)
        s.residual_impact = result.residual_impact
        s.residual_likelihood = result.residual_likelihood
        s.score_band = result.band
        scored.append((s, result))
        inherent_impacts[s.code] = s.inherent_impact

    db.commit()

    agg = aggregate([r for _, r in scored], inherent_impacts)

    scenario_reads = [
        ScenarioScoreRead(
            code=s.code,
            name=s.name,
            band=res.band,
            residual_impact=res.residual_impact,
            residual_likelihood=res.residual_likelihood,
            inherent_impact=s.inherent_impact,
            inherent_likelihood=s.inherent_likelihood,
            coverage_index=res.coverage_index,
            likelihood_reduction=res.likelihood_reduction,
            uplift=res.uplift,
            distinct_high_critical=res.distinct_high_critical,
            auditor_tested_high_critical=res.auditor_tested_high_critical,
            confidence=res.confidence,
            state_downgrades=list(res.state_downgrades),
            rationale=s.rationale,
        )
        for s, res in scored
    ]
    aggregate_read = AggregateScoreRead(
        band=agg.band,
        rank=agg.rank,
        weighted_mean_rank=agg.weighted_mean_rank,
        top2_mean_rank=agg.top2_mean_rank,
        confidence=agg.confidence,
    )
    return scenario_reads, aggregate_read


@router.post("/{assessment_id}/recalculate")
def recalculate(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    scenarios, agg = _recalculate_in_session(db, a)
    return {"scenarios": [s.model_dump() for s in scenarios], "aggregate": agg.model_dump()}


@router.post("/{assessment_id}/narratives/run", response_model=TaskStatusRead)
async def run_narratives(assessment_id: int, db: Session = Depends(db_session)):
    """Generate score-explanation prose for every scenario, then the
    executive summary. Requires gap analysis to be done and current (a
    partial gap analysis — some controls failed — still counts; the warning
    is repeated on the phase). Re-attaches to a run already in flight."""
    a = get_assessment(assessment_id, db)
    workflow.require_step_ready(a, "narratives")
    live = workflow.require_no_run_in_flight(a, reattach_kind=workflow.KIND_NARRATIVES)
    if live is not None:
        return workflow.reattach_response(live)

    aid = a.id

    async def job(handle):
        from app.ai.agents import executive_summary as summary_agent
        from app.ai.agents import narrative as narr_agent
        try:
            with SessionLocal() as inner:
                assessment = inner.get(Assessment, aid)
                scenarios = list(assessment.scenarios)
                total = max(1, len(scenarios))
                for i, s in enumerate(scenarios, start=1):
                    await narr_agent.write_for_scenario(inner, assessment, s)
                    await handle.update(progress=0.9 * i / total, detail=s.code)
                await handle.update(progress=0.9, detail="Writing executive summary")
                await summary_agent.write(inner, aid)
                inner.commit()
            mark_phase_done(aid, "narratives")
        except Exception as e:
            mark_phase_error(aid, "narratives", str(e))
            raise

    handle = registry.submit(job, kind=workflow.KIND_NARRATIVES, assessment_id=aid)
    mark_phase_started(aid, "narratives", handle.id)
    return workflow.reattach_response(handle)


@router.post("/{assessment_id}/executive-summary/run", response_model=TaskStatusRead)
async def run_executive_summary(assessment_id: int, db: Session = Depends(db_session)):
    """(Re)generate only the executive summary, e.g. after edits made it
    stale. Same prerequisites as narratives (it does not need the narrative
    prose); refused while any job runs."""
    a = get_assessment(assessment_id, db)
    workflow.require_step_ready(a, "narratives")
    workflow.require_no_run_in_flight(a)
    aid = a.id

    async def job(handle):
        from app.ai.agents import executive_summary as summary_agent
        with SessionLocal() as inner:
            await handle.update(progress=0.1, detail="Writing executive summary")
            await summary_agent.write(inner, aid)
        await handle.update(progress=1.0, detail="Executive summary updated")

    handle = registry.submit(job, kind=workflow.KIND_EXEC_SUMMARY, assessment_id=aid)
    return TaskStatusRead(task_id=handle.id, status=handle.status, progress=0.0, detail="")
