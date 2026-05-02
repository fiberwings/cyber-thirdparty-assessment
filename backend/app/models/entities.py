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
    weaknesses: Mapped[list["Weakness"]] = relationship(
        back_populates="assessment", cascade="all, delete-orphan"
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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    assessment: Mapped[Assessment] = relationship(back_populates="documents")
    chunks: Mapped[list["Chunk"]] = relationship(
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
    source: Mapped[str] = mapped_column(String(20), default="description")  # description|emergent
    inherent_impact: Mapped[int] = mapped_column(Integer, default=2)
    inherent_likelihood: Mapped[int] = mapped_column(Integer, default=2)
    residual_impact: Mapped[int] = mapped_column(Integer, default=2)
    residual_likelihood: Mapped[int] = mapped_column(Integer, default=2)
    score_band: Mapped[str] = mapped_column(String(20), default="Moderate")
    rationale: Mapped[str] = mapped_column(Text, default="")
    user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assessment_id: Mapped[int] = mapped_column(
        ForeignKey("assessment.id", ondelete="CASCADE")
    )
    source_chunk_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("chunk.id", ondelete="SET NULL"), nullable=True
    )
    severity: Mapped[str] = mapped_column(String(20), default="medium")
    description: Mapped[str] = mapped_column(Text)
    mapped_control_codes: Mapped[list] = mapped_column(JSON, default=list)
    quote: Mapped[str] = mapped_column(Text, default="")
    user_edited: Mapped[bool] = mapped_column(Boolean, default=False)

    assessment: Mapped[Assessment] = relationship(back_populates="weaknesses")
    chunk: Mapped[Optional[Chunk]] = relationship()


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
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
