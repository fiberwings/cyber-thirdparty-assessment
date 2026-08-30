from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.ai.agents import cross_correlation as corr_agent
from app.api.deps import db_session, get_assessment
from app.db import SessionLocal
from app.schemas.api import TaskStatusRead, WeaknessRead
from app.tasks import mark_phase_done, mark_phase_error, mark_phase_started, registry

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
    aid = a.id

    async def job(handle):
        await handle.update(progress=0.05, detail="Correlating weaknesses...")

        async def on_progress(p: float, detail: str):
            await handle.update(progress=p, detail=detail)

        try:
            with SessionLocal() as inner:
                await corr_agent.run(inner, aid, on_progress=on_progress)
            mark_phase_done(aid, "cross_correlation")
        except Exception as e:
            mark_phase_error(aid, "cross_correlation", str(e))
            raise

    handle = registry.submit(job, kind="cross_correlation", assessment_id=aid)
    mark_phase_started(aid, "cross_correlation", handle.id)
    return TaskStatusRead(
        task_id=handle.id, status=handle.status, progress=0.0, detail=""
    )
