from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

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
from app.tasks import registry

router = APIRouter(prefix="/api/assessments", tags=["scoring"])


def _recalculate_in_session(db: Session, a: Assessment) -> tuple[list[ScenarioScoreRead], AggregateScoreRead]:
    scored = []
    inherent_impacts = {}

    # Pre-bucket every mapped weakness by the control codes it touches so each
    # scenario can pull only the weaknesses that hit one of its expected
    # controls. Unmatched weaknesses don't count here — they're either folded
    # into emergent scenarios by cross_correlation or surfaced as advisory
    # findings, but they don't move a mapped scenario's residual.
    weaknesses_by_code: dict[str, list[tuple[str, list[str]]]] = {}
    for w in a.weaknesses:
        if w.unmatched:
            continue
        codes = list(w.mapped_control_codes or [])
        for code in codes:
            weaknesses_by_code.setdefault(code, []).append((w.severity, codes))

    for s in a.scenarios:
        ec_codes = [ec.code for ec in s.expected_controls]
        # Deduplicate weaknesses across overlapping mapped codes so one
        # weakness mapped to multiple of this scenario's controls only counts
        # once toward its uplift.
        seen: set[int] = set()
        scenario_weaknesses: list[WeaknessInput] = []
        for code in ec_codes:
            for sev, w_codes in weaknesses_by_code.get(code, []):
                key = id((sev, tuple(w_codes)))
                if key in seen:
                    continue
                seen.add(key)
                scenario_weaknesses.append(
                    WeaknessInput(severity=sev, mapped_control_codes=list(w_codes))
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
            meta_uplift=res.meta_uplift,
            weakness_uplift=res.weakness_uplift,
            effectiveness_downgrades=list(res.effectiveness_downgrades),
            rationale=s.rationale,
        )
        for s, res in scored
    ]
    aggregate_read = AggregateScoreRead(
        band=agg.band,
        rank=agg.rank,
        weighted_mean_rank=agg.weighted_mean_rank,
        top2_mean_rank=agg.top2_mean_rank,
    )
    return scenario_reads, aggregate_read


@router.post("/{assessment_id}/recalculate")
def recalculate(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    scenarios, agg = _recalculate_in_session(db, a)
    return {"scenarios": [s.model_dump() for s in scenarios], "aggregate": agg.model_dump()}


@router.post("/{assessment_id}/narratives/run", response_model=TaskStatusRead)
async def run_narratives(assessment_id: int, db: Session = Depends(db_session)):
    """Generate score-explanation prose for every scenario."""
    a = get_assessment(assessment_id, db)

    async def job(handle):
        from app.ai.agents import narrative as narr_agent
        with SessionLocal() as inner:
            assessment = inner.get(Assessment, a.id)
            scenarios = list(assessment.scenarios)
            total = max(1, len(scenarios))
            for i, s in enumerate(scenarios, start=1):
                await narr_agent.write_for_scenario(inner, assessment, s)
                await handle.update(progress=i / total, detail=s.code)
            assessment.current_phase = "score"
            inner.commit()

    handle = registry.submit(job)
    return TaskStatusRead(task_id=handle.id, status=handle.status, progress=0.0, detail="")
