from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import db_session, get_assessment
from app.api.scoring import _recalculate_in_session
from app.api.serializers import (
    serialize_assessment,
    serialize_description,
    serialize_document,
)
from app.schemas.api import MetaIssueRead, ReportOut, WeaknessRead

router = APIRouter(prefix="/api/assessments", tags=["report"])


@router.get("/{assessment_id}/report", response_model=ReportOut)
def report(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    scenarios, agg = _recalculate_in_session(db, a)
    return ReportOut(
        assessment=serialize_assessment(a),
        description=serialize_description(a.description),
        documents=[serialize_document(d) for d in a.documents],
        scenarios=scenarios,
        weaknesses=[WeaknessRead.model_validate(w) for w in a.weaknesses],
        meta_issues=[MetaIssueRead.model_validate(m) for m in a.meta_issues],
        aggregate=agg,
    )
