"""Per-document weakness extraction + cross-correlation endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.ai.agents import cross_correlation as corr_agent
from app.ai.agents import document_weaknesses as docw_agent
from app.api.deps import db_session, get_assessment
from app.db import SessionLocal
from app.models import Document, Weakness
from app.schemas.api import (
    FindingRead,
    TaskStatusRead,
    WeaknessRead,
)
from app.tasks import registry

router = APIRouter(prefix="/api", tags=["document-weaknesses"])


@router.post(
    "/documents/{document_id}/extract-weaknesses",
    response_model=TaskStatusRead,
)
async def extract_weaknesses(
    document_id: int, db: Session = Depends(db_session)
):
    d = db.get(Document, document_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Document not found")

    async def job(handle):
        await handle.update(progress=0.05, detail="Loading document...")

        async def on_progress(p: float, detail: str):
            await handle.update(progress=p, detail=detail)

        # Each task gets its own session.
        with SessionLocal() as inner:
            await docw_agent.extract(
                inner, document_id, on_progress=on_progress
            )

    handle = registry.submit(job, kind="document_extraction", assessment_id=d.assessment_id)
    return TaskStatusRead(
        task_id=handle.id, status=handle.status, progress=0.0, detail=""
    )


@router.get(
    "/documents/{document_id}/weaknesses",
    response_model=list[WeaknessRead],
)
def list_document_weaknesses(
    document_id: int, db: Session = Depends(db_session)
):
    d = db.get(Document, document_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Document not found")
    rows = (
        db.query(Weakness)
        .filter(Weakness.source_document_id == document_id)
        .order_by(Weakness.id)
        .all()
    )
    return [WeaknessRead.model_validate(w) for w in rows]


@router.post(
    "/assessments/{assessment_id}/cross-correlate",
    response_model=TaskStatusRead,
)
async def cross_correlate(
    assessment_id: int, db: Session = Depends(db_session)
):
    a = get_assessment(assessment_id, db)

    async def job(handle):
        await handle.update(progress=0.05, detail="Correlating weaknesses...")

        async def on_progress(p: float, detail: str):
            await handle.update(progress=p, detail=detail)

        with SessionLocal() as inner:
            await corr_agent.run(
                inner, a.id, on_progress=on_progress
            )

    handle = registry.submit(job, kind="cross_correlation", assessment_id=a.id)
    return TaskStatusRead(
        task_id=handle.id, status=handle.status, progress=0.0, detail=""
    )


@router.get(
    "/assessments/{assessment_id}/findings",
    response_model=list[FindingRead],
)
def list_findings(
    assessment_id: int, db: Session = Depends(db_session)
):
    """Unmatched weaknesses — the 'findings to remediate' view."""
    a = get_assessment(assessment_id, db)
    rows = [w for w in a.weaknesses if w.unmatched]
    rows.sort(key=lambda w: ({"critical": 0, "high": 1, "medium": 2, "low": 3}.get(w.severity, 4), w.id))
    return [FindingRead.model_validate(w) for w in rows]
