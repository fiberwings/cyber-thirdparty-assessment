"""Common dependencies."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Assessment, ExpectedControl, Scenario


def db_session() -> Generator[Session, None, None]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def get_assessment(assessment_id: int, db: Session) -> Assessment:
    a = db.get(Assessment, assessment_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return a


def get_scenario(scenario_id: int, db: Session) -> Scenario:
    s = db.get(Scenario, scenario_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return s


def get_control(control_id: int, db: Session) -> ExpectedControl:
    c = db.get(ExpectedControl, control_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Expected control not found")
    return c
