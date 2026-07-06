from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

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
    a = get_assessment(assessment_id, db)
    if a.description is None or not a.description.text.strip():
        raise HTTPException(status_code=400, detail="Set the service description first.")
    summary = (a.description.sufficiency_json or {}).get("summary_so_far") or a.description.text

    aid = a.id

    async def job(handle):
        await handle.update(progress=0.05, detail="Generating scenario skeletons...")

        async def on_progress(p: float, detail: str):
            await handle.update(progress=p, detail=detail)

        try:
            # New session inside the background task to avoid sharing the request session.
            with SessionLocal() as inner:
                assessment = inner.get(type(a), aid)
                await scenarios_agent.generate(
                    inner, assessment, summary, on_progress=on_progress
                )
                assessment.current_phase = "evidence"
                inner.commit()
            mark_phase_done(aid, "scenarios_generation")
        except Exception as e:
            mark_phase_error(aid, "scenarios_generation", str(e))
            raise

    handle = registry.submit(job)
    mark_phase_started(aid, "scenarios_generation", handle.id)
    return TaskStatusRead(
        task_id=handle.id, status=handle.status, progress=handle.progress, detail=""
    )


@router.patch("/scenarios/{scenario_id}", response_model=ScenarioRead)
def patch_scenario(
    scenario_id: int, payload: ScenarioPatch, db: Session = Depends(db_session)
):
    s = get_scenario(scenario_id, db)
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(s, k, v)
    if data:
        s.user_edited = True
    db.commit()
    db.refresh(s)
    return serialize_scenario(s)


@router.delete("/scenarios/{scenario_id}", status_code=204)
def delete_scenario(scenario_id: int, db: Session = Depends(db_session)):
    s = get_scenario(scenario_id, db)
    db.delete(s)
    db.commit()


@router.patch("/expected-controls/{control_id}", response_model=ScenarioRead)
def patch_expected_control(
    control_id: int, payload: ExpectedControlPatch, db: Session = Depends(db_session)
):
    ec = get_control(control_id, db)
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(ec, k, v)
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
    db.commit()
    db.refresh(s)
    return serialize_scenario(s)


@router.delete("/expected-controls/{control_id}", status_code=204)
def delete_expected_control(control_id: int, db: Session = Depends(db_session)):
    ec = get_control(control_id, db)
    scenario = ec.scenario
    db.delete(ec)
    if scenario is not None:
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
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(ca, k, v)
    if "coverage" in data or "effectiveness" in data:
        ca.is_locked_by_user = True
    db.commit()
    db.refresh(ca)
    return serialize_scenario(ca.expected_control.scenario)


@router.post("/expected-controls/{control_id}/assess", response_model=ScenarioRead)
def upsert_control_assessment(
    control_id: int, payload: ControlAssessmentPatch, db: Session = Depends(db_session)
):
    """Idempotent create-or-update of a control_assessment for user-driven editing."""
    ec = get_control(control_id, db)
    ca = ec.assessment or ControlAssessment(expected_control_id=ec.id)
    data = payload.model_dump(exclude_none=True)
    for k, v in data.items():
        setattr(ca, k, v)
    if "coverage" in data or "effectiveness" in data:
        ca.is_locked_by_user = True
    if not ca.id:
        db.add(ca)
    db.commit()
    db.refresh(ec)
    return serialize_scenario(ec.scenario)
