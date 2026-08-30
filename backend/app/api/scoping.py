from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai.agents import scoping as scoping_agent
from app.api.deps import db_session, get_assessment
from app.api.serializers import serialize_description
from app.db import SessionLocal
from app.schemas.api import DescriptionRead, TaskStatusRead, TurnInput
from app.tasks import registry

router = APIRouter(prefix="/api/assessments", tags=["scoping"])


@router.post("/{assessment_id}/scoping/turn", response_model=TaskStatusRead)
async def scoping_turn(
    assessment_id: int,
    payload: TurnInput | None = None,
    db: Session = Depends(db_session),
):
    a = get_assessment(assessment_id, db)
    if a.description is None:
        raise HTTPException(
            status_code=400, detail="Set the initial description before running scoping."
        )
    answer = (payload.answer if payload else None)
    aid = a.id

    async def job(handle):
        # New session inside the background task to avoid sharing the request session.
        with SessionLocal() as inner:
            assessment = inner.get(type(a), aid)
            if assessment is None:
                raise ValueError("Assessment was deleted.")
            await handle.update(progress=0.1, detail="Recording answer")
            await handle.update(progress=0.3, detail="Analyzing scope…")
            await scoping_agent.run_turn(inner, assessment, answer)
            await handle.update(progress=0.95, detail="Saving")

    handle = registry.submit(job, kind="scoping_turn", assessment_id=aid)
    return TaskStatusRead(
        task_id=handle.id, status=handle.status, progress=0.0, detail=""
    )


@router.post("/{assessment_id}/scoping/force-continue", response_model=DescriptionRead)
def force_continue(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    if a.description is None:
        raise HTTPException(status_code=400, detail="No description.")
    a.force_continued = True
    a.description.is_sufficient = True
    if a.current_phase == "scoping":
        a.current_phase = "scenarios"
    db.commit()
    db.refresh(a)
    return serialize_description(a.description)
