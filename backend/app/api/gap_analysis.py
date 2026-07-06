from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai.agents import gap_analysis as gap_agent
from app.api.deps import db_session, get_assessment
from app.db import SessionLocal
from app.models import Assessment
from app.schemas.api import TaskStatusRead
from app.tasks import mark_phase_done, mark_phase_error, mark_phase_started, registry

router = APIRouter(prefix="/api/assessments", tags=["gap-analysis"])


@router.post("/{assessment_id}/gap-analysis/run", response_model=TaskStatusRead)
async def run_gap_analysis(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    if not a.scenarios:
        raise HTTPException(status_code=400, detail="Generate scenarios first.")

    aid = a.id

    async def job(handle):
        try:
            with SessionLocal() as inner:
                assessment = inner.get(Assessment, aid)

                def on_progress(done: int, total: int, label: str):
                    handle.set(progress=done / max(total, 1), detail=label)

                await gap_agent.run_full(inner, assessment, on_progress=on_progress)
                assessment.current_phase = "analysis"
                inner.commit()
            mark_phase_done(aid, "gap_analysis")
        except Exception as e:
            mark_phase_error(aid, "gap_analysis", str(e))
            raise

    handle = registry.submit(job)
    mark_phase_started(aid, "gap_analysis", handle.id)
    return TaskStatusRead(
        task_id=handle.id, status=handle.status, progress=handle.progress, detail=""
    )
