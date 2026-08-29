"""Pydantic schemas the AI agents are required to emit.

These define the contract between LLM output and our system. Validation
failures trigger a stricter retry inside `app.ai.router.call_structured`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Coverage = Literal["none", "partial", "full"]
Effectiveness = Literal["weak", "adequate", "strong", "unknown"]
# Meta-issues describe gaps in *our* evidence (assessment quality). A
# contradiction between sources is a vendor *finding* and is emitted as a
# `ContradictionOut` on the control assessment instead — never as a meta flag.
MetaKind = Literal["insufficient_info", "vague_answer", "missing_doc"]


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


class ScenarioSkeletonOut(BaseModel):
    """Phase-1 output: scenario without expected_controls."""

    code: str = Field(min_length=2, max_length=60)
    name: str = Field(min_length=2, max_length=200)
    description: str
    inherent_impact: int = Field(ge=1, le=4)
    inherent_likelihood: int = Field(ge=1, le=4)


class ScenarioSkeletonListOut(BaseModel):
    scenarios: list[ScenarioSkeletonOut] = Field(min_length=1)


class ExpectedControlListOut(BaseModel):
    """Phase-2 output: expected_controls for a single scenario."""

    expected_controls: list[ExpectedControlOut] = Field(min_length=1)


# ---------- Gap analysis (per control) ----------

class CitationOut(BaseModel):
    document_id: int
    chunk_id: int | None = None  # filled by retrieval if not returned by model
    page: int | None = None
    section_path: str = ""
    quote: str = Field(min_length=2)


class ContradictionOut(BaseModel):
    """Two or more supplied sources disagree about this control.

    This is a vendor finding (their own statements, or an auditor's test,
    disagree) and is persisted as a scored `Weakness`; `claims` carries a
    verbatim quote for every side of the disagreement.
    """

    severity: Literal["low", "medium", "high", "critical"]
    description: str = Field(min_length=10)
    claims: list[CitationOut] = Field(min_length=2)

    @field_validator("claims")
    @classmethod
    def claims_must_span_sources(cls, v):
        locs = {(c.document_id, c.section_path, c.page) for c in v}
        if len(locs) < 2:
            raise ValueError(
                "A contradiction needs at least two distinct sources "
                "(different documents, or different sections of one document)."
            )
        return v


class ControlAssessmentOut(BaseModel):
    control_code: str
    coverage: Coverage
    effectiveness: Effectiveness
    citations: list[CitationOut] = Field(default_factory=list)
    rationale: str
    meta_flags: list[MetaKind] = Field(default_factory=list)
    contradictions: list[ContradictionOut] = Field(default_factory=list)
    # Alternative search queries the model proposes when the candidate
    # evidence was insufficient — drives one bounded second retrieval pass.
    proposed_queries: list[str] = Field(default_factory=list, max_length=4)

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


# ---------- Per-document weakness extraction ----------

Severity = Literal["low", "medium", "high", "critical"]
KindSignal = Literal[
    "pentest_finding",
    "soc_exception",
    "iso_nonconformity",
    "policy_gap",
    "questionnaire_negative",
    "dpa_clause_missing",
    "cross_doc_conflict",  # set by gap analysis, never by per-document extraction
    "other",
]


class DocumentWeaknessOut(BaseModel):
    """One concrete finding extracted from a single document."""

    severity: Severity
    description: str = Field(min_length=4)
    quote: str = Field(default="", max_length=2000)
    section_path: str = ""
    page: int | None = None
    kind_signal: KindSignal
    # Codes the model thinks could plausibly be related; cross-correlation
    # is authoritative — these are hints only.
    suggested_control_codes: list[str] = Field(default_factory=list)


class DocumentWeaknessListOut(BaseModel):
    weaknesses: list[DocumentWeaknessOut] = Field(default_factory=list)


class WeaknessSkeletonOut(BaseModel):
    """Phase-1 fallback output: enumerate findings without details."""

    heading: str = Field(min_length=2, max_length=200)
    severity: Severity
    section_path: str = ""
    kind_signal: KindSignal


class WeaknessSkeletonListOut(BaseModel):
    skeletons: list[WeaknessSkeletonOut] = Field(default_factory=list)


# ---------- Cross-correlation ----------

class WeaknessMappingOut(BaseModel):
    """How a single weakness maps onto existing controls."""

    weakness_id: int
    # Empty list = unmatched; cross-correlation will leave `unmatched=true` set.
    mapped_control_codes: list[str] = Field(default_factory=list)


class WeaknessClusterMappingOut(BaseModel):
    """Per-cluster output: one or more weaknesses → mapping or new scenario."""

    weakness_mappings: list[WeaknessMappingOut] = Field(default_factory=list)
    # When the cluster doesn't fit any existing scenario's controls, propose
    # a new emergent scenario (same shape as initial scenario generation).
    propose_emergent: ScenarioOut | None = None
    # IDs of weaknesses that justify the emergent scenario; populated only
    # when propose_emergent is not None.
    origin_weakness_ids: list[int] = Field(default_factory=list)


class CrossCorrelationOut(BaseModel):
    clusters: list[WeaknessClusterMappingOut] = Field(default_factory=list)


# ---------- Executive summary ----------

class KeyRiskOut(BaseModel):
    """One of the assessment's most important risks, in priority order."""

    title: str = Field(min_length=3)
    why_it_matters: str
    # References into the supplied data — validated post-hoc by the agent.
    scenario_codes: list[str] = Field(default_factory=list)
    weakness_ids: list[int] = Field(default_factory=list)
    evidence_basis: str = ""


class RecommendedActionOut(BaseModel):
    action: str
    priority: Literal["immediate", "near_term", "monitor"]
    related_scenario_codes: list[str] = Field(default_factory=list)


class ExecutiveSummaryOut(BaseModel):
    verdict: str = Field(min_length=20)
    key_risks: list[KeyRiskOut] = Field(min_length=1, max_length=5)
    limitations: list[str] = Field(default_factory=list)
    recommended_actions: list[RecommendedActionOut] = Field(default_factory=list)
