from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.ai.agents import cross_correlation as corr_agent
from app.api.deps import db_session, get_assessment
from app.db import SessionLocal
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
    """Backward-compatible alias for `POST /assessments/{id}/cross-correlate`.

    The original synthesizer (heuristic 60-chunk pool, single global call) was
    retired in favour of the per-document extraction + cross-correlation
    pipeline. The frontend's `synthesizeWeaknesses` action now triggers
    cross-correlation over already-extracted weaknesses.
    """
    a = get_assessment(assessment_id, db)

    async def job(handle):
        await handle.update(progress=0.05, detail="Correlating weaknesses...")

        async def on_progress(p: float, detail: str):
            await handle.update(progress=p, detail=detail)

        with SessionLocal() as inner:
            await corr_agent.run(inner, a.id, on_progress=on_progress)

    handle = registry.submit(job)
    return TaskStatusRead(
        task_id=handle.id, status=handle.status, progress=0.0, detail=""
    )
