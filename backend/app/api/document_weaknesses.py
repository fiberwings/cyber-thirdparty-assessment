"""Per-document weakness listing, cross-correlation and findings endpoints.

Extraction itself (`POST /documents/{id}/extract-weaknesses`) lives in
app.api.documents next to the upload path that auto-fires it, so both share
one submit helper and one persisted task-state contract.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import db_session, get_assessment
from app.api.weaknesses import submit_correlation
from app.models import Document, Weakness
from app.schemas.api import (
    FindingRead,
    TaskStatusRead,
    WeaknessRead,
)

router = APIRouter(prefix="/api", tags=["document-weaknesses"])


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
    """Correlate extracted weaknesses across the evidence bundle. Same job as
    `/weaknesses/synthesize`: guarded, re-attaching, and writing the
    cross_correlation phase markers."""
    a = get_assessment(assessment_id, db)
    return submit_correlation(db, a)


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
