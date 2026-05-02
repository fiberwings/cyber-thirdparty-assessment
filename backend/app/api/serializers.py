"""ORM → API DTO converters (the simple read paths don't perfectly map via from_attributes
because of nested/JSON shapes)."""

from __future__ import annotations

from app.models import (
    Assessment,
    ControlAssessment,
    ControlEvidence,
    ExpectedControl,
    Scenario,
    ServiceDescription,
    Document,
)
from app.schemas.api import (
    AssessmentRead,
    CitationRead,
    ControlAssessmentRead,
    DescriptionRead,
    DocumentRead,
    ExpectedControlRead,
    ScenarioRead,
    TurnRead,
)


def serialize_evidence(ev: ControlEvidence) -> CitationRead:
    chunk = ev.chunk
    return CitationRead(
        document_id=chunk.document_id if chunk else 0,
        chunk_id=chunk.id if chunk else 0,
        page=chunk.page if chunk else None,
        section_path=chunk.section_path if chunk else "",
        quote=ev.quote,
        polarity=ev.polarity,
    )


def serialize_control_assessment(ca: ControlAssessment | None) -> ControlAssessmentRead | None:
    if ca is None:
        return None
    return ControlAssessmentRead(
        id=ca.id,
        coverage=ca.coverage,
        effectiveness=ca.effectiveness,
        rationale=ca.rationale,
        is_locked_by_user=ca.is_locked_by_user,
        citations=[serialize_evidence(e) for e in ca.evidence],
    )


def serialize_expected_control(ec: ExpectedControl) -> ExpectedControlRead:
    return ExpectedControlRead(
        id=ec.id,
        code=ec.code,
        name=ec.name,
        description=ec.description,
        weight=ec.weight,
        rationale=ec.rationale,
        assessment=serialize_control_assessment(ec.assessment),
    )


def serialize_scenario(s: Scenario) -> ScenarioRead:
    return ScenarioRead(
        id=s.id,
        code=s.code,
        name=s.name,
        description=s.description,
        source=s.source,
        inherent_impact=s.inherent_impact,
        inherent_likelihood=s.inherent_likelihood,
        residual_impact=s.residual_impact,
        residual_likelihood=s.residual_likelihood,
        score_band=s.score_band,
        rationale=s.rationale,
        user_edited=s.user_edited,
        expected_controls=[serialize_expected_control(ec) for ec in s.expected_controls],
    )


def serialize_description(d: ServiceDescription | None) -> DescriptionRead | None:
    if d is None:
        return None
    return DescriptionRead(
        text=d.text,
        is_sufficient=d.is_sufficient,
        sufficiency_json=d.sufficiency_json or {},
        turns=[
            TurnRead(id=t.id, role=t.role, content=t.content, created_at=t.created_at)
            for t in d.turns
        ],
    )


def serialize_assessment(a: Assessment) -> AssessmentRead:
    return AssessmentRead.model_validate(a)


def serialize_document(d: Document) -> DocumentRead:
    return DocumentRead.model_validate(d)
