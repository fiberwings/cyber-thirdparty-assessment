from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai.agents import gap_analysis as gap_agent
from app.api.deps import db_session, get_assessment
from app.db import SessionLocal
from app.models import Assessment
from app.schemas.api import TaskStatusRead
from app.tasks import registry

router = APIRouter(prefix="/api/assessments", tags=["gap-analysis"])


@router.post("/{assessment_id}/gap-analysis/run", response_model=TaskStatusRead)
async def run_gap_analysis(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    if not a.scenarios:
        raise HTTPException(status_code=400, detail="Generate scenarios first.")

    async def job(handle):
        with SessionLocal() as inner:
            assessment = inner.get(Assessment, a.id)

            def on_progress(done: int, total: int, label: str):
                handle.set(progress=done / max(total, 1), detail=label)

            await gap_agent.run_full(inner, assessment, on_progress=on_progress)
            assessment.current_phase = "analysis"
            inner.commit()

    handle = registry.submit(job)
    return TaskStatusRead(
        task_id=handle.id, status=handle.status, progress=handle.progress, detail=""
    )
