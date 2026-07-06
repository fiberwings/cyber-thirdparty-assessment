from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.ai.agents.executive_summary import compute_fingerprint
from app.api.deps import db_session, get_assessment
from app.api.scoring import _recalculate_in_session
from app.api.serializers import (
    serialize_assessment,
    serialize_description,
    serialize_document,
)
from app.models import Assessment
from app.schemas.api import (
    ExecutiveSummaryRead,
    MetaIssueRead,
    ReportOut,
    WeaknessRead,
)

router = APIRouter(prefix="/api/assessments", tags=["report"])


def _serialize_executive_summary(a: Assessment) -> ExecutiveSummaryRead | None:
    """Stored summary + staleness vs the freshly recalculated state.

    A stale summary is still returned (it may be the only one there is) — the
    UI shows a prominent "scores changed since this summary" banner instead of
    silently re-spending a reasoner call on every GET.
    """
    blob = a.executive_summary
    if not blob or not blob.get("summary"):
        return None
    summary = blob["summary"]
    return ExecutiveSummaryRead(
        generated_at=blob.get("generated_at", ""),
        model_id=blob.get("model_id", ""),
        stale=blob.get("fingerprint") != compute_fingerprint(a),
        verdict=summary.get("verdict", ""),
        key_risks=summary.get("key_risks", []),
        limitations=summary.get("limitations", []),
        recommended_actions=summary.get("recommended_actions", []),
    )


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
        executive_summary=_serialize_executive_summary(a),
    )
