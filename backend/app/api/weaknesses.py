from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.ai.agents import weaknesses as weak_agent
from app.api.deps import db_session, get_assessment
from app.db import SessionLocal
from app.models import Assessment, Weakness
from app.schemas.api import TaskStatusRead, WeaknessRead
from app.tasks import registry

router = APIRouter(prefix="/api/assessments", tags=["weaknesses"])


@router.get("/{assessment_id}/weaknesses", response_model=list[WeaknessRead])
def list_weaknesses(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    return [WeaknessRead.model_validate(w) for w in a.weaknesses]


@router.post(
    "/{assessment_id}/weaknesses/synthesize",
    response_model=TaskStatusRead,
)
async def synthesize(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    summary = (a.description.text if a.description else "") if a.description else ""

    async def job(handle):
        with SessionLocal() as inner:
            assessment = inner.get(Assessment, a.id)
            await handle.update(progress=0.2, detail="Reviewing evidence pool")
            await weak_agent.synthesize(inner, assessment, summary)
            inner.commit()

    handle = registry.submit(job)
    return TaskStatusRead(task_id=handle.id, status=handle.status, progress=0.0, detail="")
