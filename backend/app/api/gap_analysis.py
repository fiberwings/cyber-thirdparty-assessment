from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import workflow
from app.ai.agents import gap_analysis as gap_agent
from app.api.deps import db_session, get_assessment, get_control
from app.api.serializers import serialize_scenario
from app.db import SessionLocal
from app.models import Assessment, ExpectedControl, Scenario
from app.schemas.api import ScenarioRead, TaskStatusRead
from app.tasks import (
    mark_phase_done,
    mark_phase_error,
    mark_phase_started,
    registry,
    update_failed_targets,
)

router = APIRouter(prefix="/api", tags=["gap-analysis"])


@router.post("/assessments/{assessment_id}/gap-analysis/run", response_model=TaskStatusRead)
async def run_gap_analysis(
    assessment_id: int, only_failed: bool = False, db: Session = Depends(db_session)
):
    """Run gap analysis over every scenario × control.

    Requires scoping, scenarios, evidence extraction and cross-correlation to
    be done and current (gap analysis reads the correlated weakness set so it
    does not re-emit them as contradictions).

    `only_failed=true` resumes a previous run: only controls whose last AI
    run failed (or that were never assessed) are re-run. It is refused when
    there is no run to resume or when inputs changed in a way a partial
    re-run cannot fix (409 `resume_requires_full_run`). A run with some
    failed controls still completes the phase — the failures are persisted
    per control (ControlAssessment.last_error), listed on the phase as
    `failed_targets`, and resumable here or per control below.
    """
    a = get_assessment(assessment_id, db)
    workflow.require_step_ready(a, "analysis", resume=only_failed)
    live = workflow.require_no_run_in_flight(a, reattach_kind=workflow.KIND_GAP)
    if live is not None:
        return workflow.reattach_response(live)
    workflow.invalidate_downstream(a, "narratives", reason="Gap analysis re-run")
    db.commit()

    aid = a.id

    async def job(handle):
        try:
            with SessionLocal() as inner:
                assessment = inner.get(Assessment, aid)

                def on_progress(done: int, total: int, label: str):
                    handle.set(progress=done / max(total, 1), detail=label)

                result = await gap_agent.run_full(
                    inner, assessment, on_progress=on_progress, only_failed=only_failed
                )
                inner.commit()
                inner.refresh(assessment)
                still_failed = gap_agent.failed_targets(assessment)
            warning = result.warning
            if still_failed and not warning:
                warning = (
                    f"{len(still_failed)} control(s) still carry a failed AI run "
                    f"({', '.join(still_failed[:6])}{'…' if len(still_failed) > 6 else ''})"
                )
            mark_phase_done(aid, "gap_analysis", warning=warning, failed_targets=still_failed)
            if warning:
                handle.set(detail=warning)
        except Exception as e:
            mark_phase_error(aid, "gap_analysis", str(e))
            raise

    handle = registry.submit(job, kind=workflow.KIND_GAP, assessment_id=aid)
    mark_phase_started(aid, "gap_analysis", handle.id)
    return workflow.reattach_response(handle)


@router.post("/expected-controls/{control_id}/assess-ai", response_model=TaskStatusRead)
async def assess_control_ai(control_id: int, db: Session = Depends(db_session)):
    """Re-run the AI gap analysis for ONE expected control (R8): the resume
    path for a failed control, or a targeted re-assessment after new
    evidence. Respects user locks (a user-edited verdict is not overwritten).

    Same prerequisites as `gap-analysis/run?only_failed=true`; refused while
    any job runs for the assessment."""
    control = get_control(control_id, db)
    scenario = control.scenario
    a = scenario.assessment
    workflow.require_step_ready(a, "analysis", resume=True)
    workflow.require_no_run_in_flight(a)
    aid = a.id
    sid, cid = scenario.id, control.id
    label = f"{scenario.code}/{control.code}"
    workflow.invalidate_downstream(a, "narratives", reason=f"Control {label} re-assessed")
    db.commit()

    async def job(handle):
        handle.set(progress=0.1, detail=f"Assessing {label}")
        with SessionLocal() as inner:
            assessment = inner.get(Assessment, aid)
            sc = inner.get(Scenario, sid)
            ctrl = inner.get(ExpectedControl, cid)
            try:
                await gap_agent.assess_control_any_mode(inner, assessment, sc, ctrl)
            except Exception as e:
                inner.rollback()
                gap_agent.record_control_failure(cid, e)
                raise
            inner.refresh(assessment)
            still_failed = gap_agent.failed_targets(assessment)
        # Keep the phase's failed list in step with the per-control state.
        state_warning = (
            f"{len(still_failed)} control(s) still carry a failed AI run" if still_failed else None
        )
        update_failed_targets(aid, still_failed, state_warning)
        handle.set(progress=1.0, detail=f"Assessed {label}")

    handle = registry.submit(job, kind=workflow.KIND_GAP_CONTROL, assessment_id=aid)
    return TaskStatusRead(task_id=handle.id, status=handle.status, progress=0.0, detail="")


@router.get("/expected-controls/{control_id}/scenario", response_model=ScenarioRead)
def control_scenario(control_id: int, db: Session = Depends(db_session)):
    """Convenience read after a per-control re-run."""
    control = get_control(control_id, db)
    return serialize_scenario(control.scenario)
