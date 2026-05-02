from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.deps import db_session, get_assessment
from app.api.serializers import serialize_assessment, serialize_description
from app.models import Assessment, ServiceDescription
from app.schemas.api import (
    AssessmentCreate,
    AssessmentRead,
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
    if a.description is None:
        d = ServiceDescription(assessment_id=a.id, text=payload.text)
        db.add(d)
    else:
        a.description.text = payload.text
        a.description.is_sufficient = False
        a.description.sufficiency_json = {}
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
        overrides[k] = v
    a.model_overrides = overrides
    db.commit()
    db.refresh(a)
    return serialize_assessment(a)
