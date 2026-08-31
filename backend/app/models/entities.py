from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.utcnow()


class Assessment(Base):
    __tablename__ = "assessment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vendor_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(40), default="scoping")
    current_phase: Mapped[str] = mapped_column(String(40), default="scoping")
    force_continued: Mapped[bool] = mapped_column(Boolean, default=False)
    model_overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-phase run state for the four long-running orchestrators
    # (scenarios_generation, cross_correlation, gap_analysis, narratives).
    # Shape per phase: {started_at, completed_at, task_id, error}.
    # Other phases (scoping, evidence, score) are derived from data presence.
    phase_state: Mapped[dict] = mapped_column(JSON, default=dict)
    # Persisted AI executive summary:
    # {generated_at, model_id, fingerprint, summary: ExecutiveSummaryOut dump}.
    # `fingerprint` hashes the scored state so the API can flag staleness.
    executive_summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Date the assessment is performed "as of" (ISO yyyy-mm-dd). Every prompt's
    # "Analysis date" and every freshness computation use it — never the wall
    # clock — so a review anchored on a past date does not flag evidence as
    # stale against the run date. NULL = today at the time of each call.
    as_of_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # Assessor (client-side) standards profile: required attestations,
    # refresh windows, retention target, residency, MFA policy… See
    # app.schemas.standards.StandardsProfile. {} = none supplied.
    standards_profile: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=_now
    )

    description: Mapped[Optional["ServiceDescription"]] = relationship(
        back_populates="assessment", uselist=False, cascade="all, delete-orphan"
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )
    scenarios: Mapped[list["Scenario"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )
    # Every weakness row regardless of review status (owns the cascade).
    all_weaknesses: Mapped[list["Weakness"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )
    # Reported weaknesses only — the rows that score, map, and appear in the
    # report. Candidates awaiting review, evidence notes, dropped and merged
    # rows are excluded (see Weakness.status).
    weaknesses: Mapped[list["Weakness"]] = relationship(
        primaryjoin="and_(Weakness.assessment_id == Assessment.id, Weakness.status == 'confirmed')",
        viewonly=True,
    )
    meta_issues: Mapped[list["MetaIssue"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
    )


class ServiceDescription(Base):
    __tablename__ = "service_description"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE"), unique=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    sufficiency_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_sufficient: Mapped[bool] = mapped_column(Boolean, default=False)

    assessment: Mapped[Assessment] = relationship(back_populates="description")
    turns: Mapped[list["DescriptionTurn"]] = relationship(
        back_populates="description",
        cascade="all, delete-orphan",
        order_by="DescriptionTurn.id",
    )


class DescriptionTurn(Base):
    __tablename__ = "description_turn"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    description_id: Mapped[int] = mapped_column(
        ForeignKey("service_description.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(String(20))  # "user" | "ai"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    description: Mapped[ServiceDescription] = relationship(back_populates="turns")


class Document(Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(40))  # questionnaire/soc/iso/pentest/policy/other
    filename: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(120))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    parsed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    weakness_extracted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Typed attestation profile (SOC / ISO / pen-test docs only; Phase 4).
    # Shape: schemas.attestation.AttestationProfileOut dump — every field
    # carries the verbatim quote it came from.
    attestation_profile: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    assessment: Mapped[Assessment] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    weaknesses: Mapped[list["Weakness"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Chunk(Base):
    __tablename__ = "chunk"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), index=True
    )
    page: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    section_path: Mapped[str] = mapped_column(String(400), default="")
    ord: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    document: Mapped[Document] = relationship(back_populates="chunks")


class Scenario(Base):
    __tablename__ = "scenario"
    __table_args__ = (
        UniqueConstraint("assessment_id", "code", name="uq_scenario_code_per_assessment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE")
    )
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    # description | emergent | emergent_from_weakness
    source: Mapped[str] = mapped_column(String(40), default="description")
    inherent_impact: Mapped[int] = mapped_column(Integer, default=2)
    inherent_likelihood: Mapped[int] = mapped_column(Integer, default=2)
    residual_impact: Mapped[int] = mapped_column(Integer, default=2)
    residual_likelihood: Mapped[int] = mapped_column(Integer, default=2)
    score_band: Mapped[str] = mapped_column(String(20), default="Moderate")
    rationale: Mapped[str] = mapped_column(Text, default="")
    user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    origin_weakness_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    assessment: Mapped[Assessment] = relationship(back_populates="scenarios")
    expected_controls: Mapped[list["ExpectedControl"]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )


class ExpectedControl(Base):
    __tablename__ = "expected_control"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[int] = mapped_column(
        ForeignKey("scenario.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    rationale: Mapped[str] = mapped_column(Text, default="")

    scenario: Mapped[Scenario] = relationship(back_populates="expected_controls")
    assessment: Mapped[Optional["ControlAssessment"]] = relationship(
        back_populates="expected_control",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ControlAssessment(Base):
    __tablename__ = "control_assessment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expected_control_id: Mapped[int] = mapped_column(
        ForeignKey("expected_control.id", ondelete="CASCADE"), unique=True
    )
    coverage: Mapped[str] = mapped_column(String(20), default="none")  # none|partial|full
    effectiveness: Mapped[str] = mapped_column(String(20), default="unknown")
    rationale: Mapped[str] = mapped_column(Text, default="")
    is_locked_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    # Citations the model returned whose quote could not be located in any
    # chunk of the cited document: [{document_id, page, section_path, quote}].
    # Kept verbatim instead of being bound to a wrong chunk.
    unresolved_citations: Mapped[list] = mapped_column(JSON, default=list)
    # Last AI run outcome for this control. `last_error` is set when the
    # structured call failed (the verdict above is then stale or absent) and
    # cleared on the next successful run; the phase no longer fails as a
    # whole — failed controls are resumable individually.
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=_now
    )

    expected_control: Mapped[ExpectedControl] = relationship(back_populates="assessment")
    evidence: Mapped[list["ControlEvidence"]] = relationship(
        back_populates="control_assessment", cascade="all, delete-orphan"
    )


class ControlEvidence(Base):
    __tablename__ = "control_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    control_assessment_id: Mapped[int] = mapped_column(
        ForeignKey("control_assessment.id", ondelete="CASCADE"), index=True
    )
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunk.id", ondelete="CASCADE"))
    polarity: Mapped[str] = mapped_column(String(20), default="supports")  # supports|contradicts
    quote: Mapped[str] = mapped_column(Text)
    ai_rationale: Mapped[str] = mapped_column(Text, default="")

    control_assessment: Mapped[ControlAssessment] = relationship(back_populates="evidence")
    chunk: Mapped[Chunk] = relationship()


class Weakness(Base):
    __tablename__ = "weakness"
    __table_args__ = (
        UniqueConstraint("assessment_id", "dedupe_key", name="uq_weakness_dedupe"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE")
    )
    source_chunk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chunk.id", ondelete="SET NULL"), nullable=True
    )
    source_document_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("document.id", ondelete="CASCADE"), nullable=True, index=True
    )
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    description: Mapped[str] = mapped_column(Text)
    mapped_control_codes: Mapped[list] = mapped_column(JSON, default=list)
    quote: Mapped[str] = mapped_column(Text, default="")
    unmatched: Mapped[bool] = mapped_column(Boolean, default=True)
    # Free-form classifier label set by document_weaknesses agent
    # (e.g. "pentest_finding", "soc_exception", "policy_gap").
    kind_signal: Mapped[str] = mapped_column(String(40), default="")
    # sha256 of (assessment_id + normalised quote + chunk_id) — DB-enforced
    # uniqueness per assessment so concurrent / overlapping windows can't
    # double-insert the same finding. Nullable for legacy rows.
    dedupe_key: Mapped[Optional[str]] = mapped_column(String(120), nullable=True, index=True)
    user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, server_default=func.now()
    )
    # Which pipeline step created the row: "document" (per-document
    # extraction) or "gap_analysis" (cross-document contradiction spotted
    # while assessing a control).
    origin: Mapped[str] = mapped_column(String(30), default="document")
    # Every source location backing the finding — a contradiction has one
    # entry per disagreeing side: {document_id, chunk_id|null, page,
    # section_path, quote}. source_document_id / source_chunk_id mirror the
    # first entry for single-source deep links.
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    # "SCENARIO/CONTROL" targets whose gap analysis raised this row. Used to
    # keep re-runs idempotent (a target re-run drops its claim; a row with no
    # remaining claimants is removed).
    origin_refs: Mapped[list] = mapped_column(JSON, default=list)
    # Review status (R3/R4): "candidate" (extracted, not yet reviewed against
    # the bundle) | "confirmed" (reported, scored) | "evidence_note" (true
    # and useful context, not a deficiency) | "dropped" (unsupported or
    # misread) | "merged" (same deficiency as another row; see review).
    # Rows created by gap analysis or by users are confirmed directly.
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    # Audit trail of the review: {decision, reason, confidence, model_id, at,
    # merged_into?, members?}. Every non-confirmed status carries a reason.
    review: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    assessment: Mapped[Assessment] = relationship(back_populates="all_weaknesses")
    chunk: Mapped[Optional[Chunk]] = relationship()
    document: Mapped[Optional[Document]] = relationship(back_populates="weaknesses")


class MetaIssue(Base):
    __tablename__ = "meta_issue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(40))  # insufficient_info|vague_answer|missing_doc|conflicting_evidence
    target_ref: Mapped[str] = mapped_column(String(200), default="")
    weight: Mapped[float] = mapped_column(Float, default=0.5)
    rationale: Mapped[str] = mapped_column(Text, default="")
    scenario_code: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)

    assessment: Mapped[Assessment] = relationship(back_populates="meta_issues")


class ModelCall(Base):
    __tablename__ = "model_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE"), nullable=True
    )
    purpose: Mapped[str] = mapped_column(String(60))
    profile: Mapped[str] = mapped_column(String(20))  # fast|reasoner
    model_id: Mapped[str] = mapped_column(String(120))
    prompt_sha: Mapped[str] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    # True when the response came from the dev-only LLM cache (no tokens spent).
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LlmCacheEntry(Base):
    """Dev-only response cache (see Settings.llm_dev_cache). One row per
    (model, messages, sampling params) hash; stores the raw provider response."""

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(120))
    prompt_sha: Mapped[str] = mapped_column(String(64), index=True)
    response_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TaskRecord(Base):
    """Durable mirror of the in-memory task registry so task status survives
    a server restart (tasks that were running are marked interrupted)."""

    __tablename__ = "task"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(60), default="")
    assessment_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=_now
    )
