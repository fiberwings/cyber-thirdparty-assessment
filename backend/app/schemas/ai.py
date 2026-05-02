"""Pydantic schemas the AI agents are required to emit.

These define the contract between LLM output and our system. Validation
failures trigger a stricter retry inside `app.ai.router.call_structured`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Coverage = Literal["none", "partial", "full"]
Effectiveness = Literal["weak", "adequate", "strong", "unknown"]
MetaKind = Literal["insufficient_info", "vague_answer", "missing_doc", "conflicting_evidence"]


# ---------- Scoping ----------

class SufficiencyBreakdown(BaseModel):
    data_types: int = Field(ge=0, le=5)
    hosting: int = Field(ge=0, le=5)
    network_access: int = Field(ge=0, le=5)
    identity_flow: int = Field(ge=0, le=5)
    regulatory_scope: int = Field(ge=0, le=5)
    geography: int = Field(ge=0, le=5)
    criticality: int = Field(ge=0, le=5)


class ScopingTurnOut(BaseModel):
    is_sufficient: bool
    sufficiency_breakdown: SufficiencyBreakdown
    missing_dimensions: list[str] = Field(default_factory=list)
    next_question: str | None = None
    summary_so_far: str = ""

    @field_validator("next_question")
    @classmethod
    def question_iff_insufficient(cls, v, info):
        # Allow None when sufficient. We don't strictly validate here because
        # the API layer enforces this; the LLM occasionally still proposes a
        # question even when sufficient and we can ignore it.
        return v


# ---------- Scenarios ----------

class ExpectedControlOut(BaseModel):
    code: str = Field(min_length=2, max_length=60)
    name: str = Field(min_length=2, max_length=200)
    description: str = ""
    weight: float = Field(default=1.0, ge=0.0, le=2.0)
    rationale: str = ""


class ScenarioOut(BaseModel):
    code: str = Field(min_length=2, max_length=60)
    name: str = Field(min_length=2, max_length=200)
    description: str
    inherent_impact: int = Field(ge=1, le=4)
    inherent_likelihood: int = Field(ge=1, le=4)
    expected_controls: list[ExpectedControlOut] = Field(min_length=1)


class ScenarioListOut(BaseModel):
    scenarios: list[ScenarioOut] = Field(min_length=1)


# ---------- Gap analysis (per control) ----------

class CitationOut(BaseModel):
    document_id: int
    chunk_id: int | None = None  # filled by retrieval if not returned by model
    page: int | None = None
    section_path: str = ""
    quote: str = Field(min_length=2)


class ControlAssessmentOut(BaseModel):
    control_code: str
    coverage: Coverage
    effectiveness: Effectiveness
    citations: list[CitationOut] = Field(default_factory=list)
    rationale: str
    meta_flags: list[MetaKind] = Field(default_factory=list)

    @field_validator("citations")
    @classmethod
    def citations_required_when_evidenced(cls, v, info):
        coverage = info.data.get("coverage")
        if coverage in {"partial", "full"} and len(v) == 0:
            raise ValueError(
                "At least one citation (page + quote) is required when coverage "
                "is 'partial' or 'full'."
            )
        return v


# ---------- Weakness synthesis ----------

class WeaknessOut(BaseModel):
    severity: Literal["low", "medium", "high", "critical"]
    description: str = Field(min_length=4)
    quote: str = ""
    citation: CitationOut | None = None
    mapped_control_codes: list[str] = Field(default_factory=list)
    suggests_emergent_scenario_code: str | None = None  # references existing or new code


class WeaknessSynthesisOut(BaseModel):
    weaknesses: list[WeaknessOut] = Field(default_factory=list)
    emergent_scenarios: list[ScenarioOut] = Field(default_factory=list)
