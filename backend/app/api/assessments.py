from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import workflow
from app.ai.context import standards_profile
from app.ai.router import validate_model_capability
from app.api.deps import db_session, get_assessment
from app.api.serializers import serialize_assessment, serialize_description
from app.models import Assessment, ServiceDescription
from app.schemas.api import (
    AssessmentCreate,
    AssessmentRead,
    AssessmentSettingsPatch,
    DescriptionRead,
    DescriptionSet,
    ModelOverrides,
)

router = APIRouter(prefix="/api/assessments", tags=["assessments"])


@router.get("", response_model=list[AssessmentRead])
def list_assessments(db: Session = Depends(db_session)):
    rows = db.query(Assessment).order_by(Assessment.id.desc()).all()
    return [serialize_assessment(a) for a in rows]


@router.post("", response_model=AssessmentRead, status_code=201)
def create_assessment(payload: AssessmentCreate, db: Session = Depends(db_session)):
    a = Assessment(vendor_name=payload.vendor_name)
    db.add(a)
    db.commit()
    db.refresh(a)
    return serialize_assessment(a)


@router.get("/{assessment_id}", response_model=AssessmentRead)
def get_one(assessment_id: int, db: Session = Depends(db_session)):
    return serialize_assessment(get_assessment(assessment_id, db))


@router.delete("/{assessment_id}", status_code=204)
def delete_assessment(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    db.delete(a)
    db.commit()


@router.post("/{assessment_id}/description", response_model=DescriptionRead)
def set_description(
    assessment_id: int, payload: DescriptionSet, db: Session = Depends(db_session)
):
    a = get_assessment(assessment_id, db)
    workflow.require_no_run_in_flight(a)
    if a.description is None:
        d = ServiceDescription(assessment_id=a.id, text=payload.text)
        db.add(d)
    elif payload.text != a.description.text:
        a.description.text = payload.text
        a.description.is_sufficient = False
        a.description.sufficiency_json = {}
        # A changed description reopens scoping: the earlier force-continue
        # applied to the old text, and every scenario derived from it is stale.
        # Resaving identical text changes no input and is a no-op.
        a.force_continued = False
        workflow.invalidate_downstream(a, "scenarios", reason="Service description edited")
    db.commit()
    db.refresh(a)
    return serialize_description(a.description)


@router.get("/{assessment_id}/description", response_model=DescriptionRead | None)
def get_description(assessment_id: int, db: Session = Depends(db_session)):
    a = get_assessment(assessment_id, db)
    return serialize_description(a.description)


@router.patch("/{assessment_id}/model-overrides", response_model=AssessmentRead)
def patch_model_overrides(
    assessment_id: int, payload: ModelOverrides, db: Session = Depends(db_session)
):
    a = get_assessment(assessment_id, db)
    overrides = dict(a.model_overrides or {})
    for k, v in payload.model_dump(exclude_none=True).items():
        profile = "fast" if k in ("scoping", "narrative") else "reasoner"
        reason = validate_model_capability(v, profile)
        if reason is not None:
            raise HTTPException(status_code=422, detail=reason)
        overrides[k] = v
    a.model_overrides = overrides
    db.commit()
    db.refresh(a)
    return serialize_assessment(a)


@router.patch("/{assessment_id}/settings", response_model=AssessmentRead)
def patch_settings(
    assessment_id: int, payload: AssessmentSettingsPatch, db: Session = Depends(db_session)
):
    """Assessment-level inputs (R7): analysis date and assessor standards.

    Nothing is re-run here, but completed steps that consumed the old values
    are stamped stale so the assessment cannot progress on a mix of old and
    new inputs. The frontend resends the full profile, so the old and new
    values are diffed — an unchanged payload stamps nothing:

    * assessor standards feed scenario generation and every downstream agent
      → stale from scenarios
    * the analysis date feeds attestation checks, correlation and gap
      analysis → stale from correlation. Per-document extraction also reads
      it (for the evidence-age header) but is deliberately not invalidated:
      re-extracting every document for a date change is disproportionate,
      and correlation re-judges age against the new date anyway.
    """
    a = get_assessment(assessment_id, db)
    workflow.require_no_run_in_flight(a)
    old_date, old_standards = a.as_of_date, standards_profile(a)
    if payload.clear_as_of_date:
        a.as_of_date = None
    elif payload.as_of_date is not None:
        a.as_of_date = payload.as_of_date.isoformat()
    if payload.standards_profile is not None:
        a.standards_profile = payload.standards_profile.model_dump(exclude_none=True)
    if standards_profile(a) != old_standards:
        workflow.invalidate_downstream(a, "scenarios", reason="Assessor standards changed")
    if a.as_of_date != old_date:
        workflow.invalidate_downstream(
            a,
            "correlation",
            reason="Analysis date changed (document extraction is not re-run)",
        )
    db.commit()
    db.refresh(a)
    return serialize_assessment(a)
