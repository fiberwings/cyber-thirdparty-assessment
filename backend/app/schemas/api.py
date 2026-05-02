"""Pydantic schemas for the HTTP API surface."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- Assessments ----------

class AssessmentCreate(BaseModel):
    vendor_name: str = Field(min_length=1, max_length=200)


class AssessmentRead(BaseModel):
    id: int
    vendor_name: str
    status: str
    current_phase: str
    force_continued: bool
    model_overrides: dict
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DescriptionSet(BaseModel):
    text: str = Field(min_length=10)


class TurnInput(BaseModel):
    # Optional: an empty/missing answer means "ask the first / next question
    # without any user reply" — used when the user clicks 'Start scoping'.
    answer: Optional[str] = None


class TurnRead(BaseModel):
    id: int
    role: str
    content: str
    created_at: datetime

    class Config:
        from_attributes = True


class DescriptionRead(BaseModel):
    text: str
    is_sufficient: bool
    sufficiency_json: dict
    turns: list[TurnRead]

    class Config:
        from_attributes = True


# ---------- Documents ----------

class DocumentRead(BaseModel):
    id: int
    kind: str
    filename: str
    mime: str
    size_bytes: int
    parsed_at: Optional[datetime]
    weakness_extracted_at: Optional[datetime] = None
    # Populated only on the upload response so the frontend can poll the
    # background extraction task. Other endpoints return None.
    weakness_task_id: Optional[str] = None

    class Config:
        from_attributes = True


class ChunkRead(BaseModel):
    id: int
    document_id: int
    page: Optional[int]
    section_path: str
    text: str

    class Config:
        from_attributes = True


# ---------- Scenarios / Controls ----------

class CitationRead(BaseModel):
    document_id: int
    chunk_id: int
    page: Optional[int]
    section_path: str
    quote: str
    polarity: str


class ControlAssessmentRead(BaseModel):
    id: int
    coverage: str
    effectiveness: str
    rationale: str
    is_locked_by_user: bool
    citations: list[CitationRead]


class ExpectedControlRead(BaseModel):
    id: int
    code: str
    name: str
    description: str
    weight: float
    rationale: str
    assessment: Optional[ControlAssessmentRead]


class ScenarioRead(BaseModel):
    id: int
    code: str
    name: str
    description: str
    source: str
    inherent_impact: int
    inherent_likelihood: int
    residual_impact: int
    residual_likelihood: int
    score_band: str
    rationale: str
    user_edited: bool
    expected_controls: list[ExpectedControlRead]


class ScenarioPatch(BaseModel):
    inherent_impact: Optional[int] = Field(default=None, ge=1, le=4)
    inherent_likelihood: Optional[int] = Field(default=None, ge=1, le=4)
    name: Optional[str] = None
    description: Optional[str] = None


class ControlAssessmentPatch(BaseModel):
    coverage: Optional[str] = None
    effectiveness: Optional[str] = None
    rationale: Optional[str] = None
    is_locked_by_user: Optional[bool] = None


class ExpectedControlPatch(BaseModel):
    weight: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    name: Optional[str] = None
    description: Optional[str] = None
    rationale: Optional[str] = None


# ---------- Weaknesses ----------

class WeaknessRead(BaseModel):
    id: int
    severity: str
    description: str
    quote: str
    mapped_control_codes: list
    source_chunk_id: Optional[int]
    source_document_id: Optional[int] = None
    unmatched: bool = True
    kind_signal: str = ""
    user_edited: bool

    class Config:
        from_attributes = True


class FindingRead(BaseModel):
    """Unmatched weakness rendered as a remediation finding."""

    id: int
    severity: str
    description: str
    quote: str
    kind_signal: str
    source_document_id: Optional[int]
    source_chunk_id: Optional[int]

    class Config:
        from_attributes = True


# ---------- Meta-issues ----------

class MetaIssueRead(BaseModel):
    id: int
    kind: str
    target_ref: str
    weight: float
    rationale: str
    scenario_code: Optional[str]

    class Config:
        from_attributes = True


# ---------- Score / report ----------

class ScenarioScoreRead(BaseModel):
    code: str
    name: str
    band: str
    residual_impact: int
    residual_likelihood: int
    inherent_impact: int
    inherent_likelihood: int
    coverage_index: float
    likelihood_reduction: int
    meta_uplift: int
    weakness_uplift: int = 0
    effectiveness_downgrades: list[str] = Field(default_factory=list)
    rationale: str


class AggregateScoreRead(BaseModel):
    band: str
    rank: int
    weighted_mean_rank: float
    top2_mean_rank: float


class ReportOut(BaseModel):
    assessment: AssessmentRead
    description: Optional[DescriptionRead]
    documents: list[DocumentRead]
    scenarios: list[ScenarioScoreRead]
    weaknesses: list[WeaknessRead]
    meta_issues: list[MetaIssueRead]
    aggregate: AggregateScoreRead


# ---------- Models ----------

class ModelProfileRead(BaseModel):
    name: str
    default_model: str
    alternatives: list[str]


class ModelOverrides(BaseModel):
    scoping: Optional[str] = None
    scenarios: Optional[str] = None
    gap_analysis: Optional[str] = None
    weaknesses: Optional[str] = None
    narrative: Optional[str] = None


# ---------- Tasks ----------

class TaskStatusRead(BaseModel):
    task_id: str
    status: str  # pending|running|done|error
    progress: float
    detail: str
