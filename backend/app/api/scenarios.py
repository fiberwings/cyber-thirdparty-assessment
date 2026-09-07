from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import workflow
from app.ai.agents import scenarios as scenarios_agent
from app.api.deps import db_session, get_assessment, get_control, get_scenario
from app.api.serializers import serialize_scenario
from app.db import SessionLocal
from app.models import ControlAssessment, ExpectedControl
from app.schemas.api import (
    ControlAssessmentPatch,
    ExpectedControlCreate,
    ExpectedControlPatch,
    ScenarioPatch,
    ScenarioRead,
    TaskStatusRead,
)
from app.tasks import mark_phase_done, mark_phase_error, mark_phase_started, registry

router = APIRouter(prefix="/api", tags=["scenarios"])


@router.get("/assessments/{assessment_id}/scenarios", response_model=list[ScenarioRead])
def list_scenarios(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    return [serialize_scenario(s) for s in a.scenarios]


@router.post("/assessments/{assessment_id}/scenarios/generate", response_model=TaskStatusRead)
async def generate_scenarios(assessment_id: int, db: Session = Depends(db_session)):
    """Generate inherent-risk scenarios + expected controls from the scoped
    description. Requires scoping to be done; a second request while a
    generation is already running re-attaches to it (two concurrent runs
    interleave destructively — each clears and reinserts the
    description-sourced scenarios). Starting a run stamps correlation and
    everything after it stale: they were computed against the old scenario
    set."""
    a = get_assessment(assessment_id, db)
    workflow.require_step_ready(a, "scenarios")
    live = workflow.require_no_run_in_flight(a, reattach_kind=workflow.KIND_SCENARIOS)
    if live is not None:
        return workflow.reattach_response(live)
    workflow.invalidate_downstream(a, "correlation", reason="Scenarios regenerated")
    db.commit()
    summary = (a.description.sufficiency_json or {}).get("summary_so_far") or a.description.text

    aid = a.id

    async def job(handle):
        handle.plan(["Drafting scenarios", "Selecting controls"])
        handle.stage("Drafting scenarios", detail="Generating scenario skeletons...")

        async def on_progress(p: float, detail: str):
            # Progress is derived from the agent's stage/unit reports
            # (app.activity); only the human-readable detail is forwarded.
            await handle.update(detail=detail)

        try:
            # New session inside the background task to avoid sharing the request session.
            with SessionLocal() as inner:
                assessment = inner.get(type(a), aid)
                await scenarios_agent.generate(
                    inner, assessment, summary, on_progress=on_progress
                )
                inner.commit()
            mark_phase_done(aid, "scenarios_generation")
        except Exception as e:
            mark_phase_error(aid, "scenarios_generation", str(e))
            raise

    handle = registry.submit(job, kind=workflow.KIND_SCENARIOS, assessment_id=aid)
    mark_phase_started(aid, "scenarios_generation", handle.id)
    return workflow.reattach_response(handle)


# ---------- Manual edits ----------
#
# Every edit below is refused (409) while a job runs for the assessment — a
# gap-analysis worker may be writing the very rows being edited — and stamps
# the steps whose output it invalidates:
#   * scenario / control *text* feeds the gap-analysis prompt → analysis stale
#   * a *new* control was never assessed → analysis stale, resumable with
#     `only_failed=true` (it targets never-assessed controls)
#   * inherent ratings, weights, deletions and verdict edits only change the
#     deterministic score → narratives stale (recalculate is unguarded)

_SCENARIO_TEXT_FIELDS = {"name", "description"}
_CONTROL_TEXT_FIELDS = {"name", "description", "rationale"}


@router.patch("/scenarios/{scenario_id}", response_model=ScenarioRead)
def patch_scenario(
    scenario_id: int, payload: ScenarioPatch, db: Session = Depends(db_session)
):
    s = get_scenario(scenario_id, db)
    a = s.assessment
    workflow.require_no_run_in_flight(a)
    data = payload.model_dump(exclude_none=True)
    changed = {k for k, v in data.items() if getattr(s, k) != v}
    for k, v in data.items():
        setattr(s, k, v)
    if data:
        s.user_edited = True
    if changed & _SCENARIO_TEXT_FIELDS:
        workflow.invalidate_downstream(a, "analysis", reason=f"Scenario {s.code} text edited")
    elif changed:
        workflow.invalidate_downstream(a, "narratives", reason=f"Scenario {s.code} inherent rating edited")
    db.commit()
    db.refresh(s)
    return serialize_scenario(s)


@router.delete("/scenarios/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: int, db: Session = Depends(db_session)):
    s = get_scenario(scenario_id, db)
    a = s.assessment
    workflow.require_no_run_in_flight(a)
    workflow.invalidate_downstream(a, "narratives", reason=f"Scenario {s.code} deleted")
    db.delete(s)
    db.commit()


@router.patch("/expected-controls/{control_id}", response_model=ScenarioRead)
def patch_expected_control(
    control_id: int, payload: ExpectedControlPatch, db: Session = Depends(db_session)
):
    ec = get_control(control_id, db)
    a = ec.scenario.assessment
    workflow.require_no_run_in_flight(a)
    data = payload.model_dump(exclude_none=True)
    changed = {k for k, v in data.items() if getattr(ec, k) != v}
    for k, v in data.items():
        setattr(ec, k, v)
    if changed & _CONTROL_TEXT_FIELDS:
        workflow.invalidate_downstream(a, "analysis", reason=f"Control {ec.code} text edited")
    elif changed:
        workflow.invalidate_downstream(a, "narratives", reason=f"Control {ec.code} weight edited")
    db.commit()
    db.refresh(ec)
    return serialize_scenario(ec.scenario)


@router.post(
    "/scenarios/{scenario_id}/expected-controls",
    response_model=ScenarioRead,
    status_code=201,
)
def add_expected_control(
    scenario_id: int,
    payload: ExpectedControlCreate,
    db: Session = Depends(db_session),
):
    s = get_scenario(scenario_id, db)
    a = s.assessment
    workflow.require_no_run_in_flight(a)
    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(status_code=422, detail="Code is required")
    if any(ec.code == code for ec in s.expected_controls):
        raise HTTPException(
            status_code=409,
            detail=f"Control {code} already exists on this scenario",
        )
    ec = ExpectedControl(
        scenario_id=s.id,
        code=code,
        name=payload.name.strip(),
        description=payload.description,
        weight=payload.weight,
        rationale=payload.rationale,
    )
    db.add(ec)
    s.user_edited = True
    workflow.invalidate_downstream(
        a, "analysis", reason=f"Control {code} added to {s.code}", resume_ok=True
    )
    db.commit()
    db.refresh(s)
    return serialize_scenario(s)


@router.delete("/expected-controls/{control_id}", status_code=204)
def delete_expected_control(control_id: int, db: Session = Depends(db_session)):
    ec = get_control(control_id, db)
    scenario = ec.scenario
    a = scenario.assessment
    workflow.require_no_run_in_flight(a)
    workflow.invalidate_downstream(
        a, "narratives", reason=f"Control {ec.code} deleted from {scenario.code}"
    )
    db.delete(ec)
    scenario.user_edited = True
    db.commit()


@router.patch("/control-assessments/{ca_id}", response_model=ScenarioRead)
def patch_control_assessment(
    ca_id: int, payload: ControlAssessmentPatch, db: Session = Depends(db_session)
):
    ca = db.get(ControlAssessment, ca_id)
    if ca is None:
        # Create on-the-fly if user edits a never-assessed control. Look up by EC id supplied via query? Not in this signature.
        raise HTTPException(status_code=404, detail="Control assessment not found")
    ec = ca.expected_control
    a = ec.scenario.assessment
    workflow.require_no_run_in_flight(a)
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(ca, k, v)
    if "coverage" in data or "effectiveness" in data:
        ca.is_locked_by_user = True
    if data:
        workflow.invalidate_downstream(a, "narratives", reason=f"Verdict for {ec.code} edited")
    db.commit()
    db.refresh(ca)
    return serialize_scenario(ca.expected_control.scenario)


@router.post("/expected-controls/{control_id}/assess", response_model=ScenarioRead)
def upsert_control_assessment(
    control_id: int, payload: ControlAssessmentPatch, db: Session = Depends(db_session)
):
    """Idempotent create-or-update of a control_assessment for user-driven editing."""
    ec = get_control(control_id, db)
    a = ec.scenario.assessment
    workflow.require_no_run_in_flight(a)
    ca = ec.assessment or ControlAssessment(expected_control_id=ec.id)
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(ca, k, v)
    if "coverage" in data or "effectiveness" in data:
        ca.is_locked_by_user = True
    if not ca.id:
        db.add(ca)
    if data:
        workflow.invalidate_downstream(a, "narratives", reason=f"Verdict for {ec.code} edited")
    db.commit()
    db.refresh(ec)
    return serialize_scenario(ec.scenario)
