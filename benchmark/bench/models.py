"""Results DB schema (benchmark-owned SQLite; independent of the main app)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "run"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # running | done | partial (some cases errored) | failed (all errored)
    status: Mapped[str] = mapped_column(String(16), default="running")

    backend_url: Mapped[str] = mapped_column(String(256))
    app_git_sha: Mapped[str] = mapped_column(String(64), default="")
    app_git_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    app_version: Mapped[str] = mapped_column(String(32), default="")
    models_json: Mapped[str] = mapped_column(Text, default="{}")
    judge_model: Mapped[str] = mapped_column(String(128))
    judge_prompt_versions_json: Mapped[str] = mapped_column(Text, default="{}")
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    notes: Mapped[str] = mapped_column(Text, default="")

    case_results: Mapped[list["CaseResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class CaseResult(Base):
    __tablename__ = "case_result"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"))
    case_id: Mapped[str] = mapped_column(String(64))
    repetition: Mapped[int] = mapped_column(Integer, default=1)
    assessment_id: Mapped[int | None] = mapped_column(nullable=True)
    assessment_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # ok | error (pipeline failed) | judge_error (pipeline ok, grading failed)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    error_stage: Mapped[str] = mapped_column(String(32), default="")
    error_detail: Mapped[str] = mapped_column(Text, default="")

    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    timings_json: Mapped[str] = mapped_column(Text, default="{}")

    aggregate_band: Mapped[str] = mapped_column(String(16), default="")
    aggregate_rank: Mapped[int | None] = mapped_column(nullable=True)

    tp: Mapped[int | None] = mapped_column(nullable=True)
    fp: Mapped[int | None] = mapped_column(nullable=True)
    fn: Mapped[int | None] = mapped_column(nullable=True)
    precision: Mapped[float | None] = mapped_column(Float, nullable=True)
    recall: Mapped[float | None] = mapped_column(Float, nullable=True)
    f1: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity_exact: Mapped[float | None] = mapped_column(Float, nullable=True)
    severity_mae: Mapped[float | None] = mapped_column(Float, nullable=True)

    exec_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    exec_faithfulness: Mapped[float | None] = mapped_column(Float, nullable=True)
    exec_violation: Mapped[float | None] = mapped_column(Float, nullable=True)
    exec_overall: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Signal/noise classification (judge=full) — see metrics.score_classification
    signal_share: Mapped[float | None] = mapped_column(Float, nullable=True)
    dup_per_golden: Mapped[float | None] = mapped_column(Float, nullable=True)
    judge_fn: Mapped[int | None] = mapped_column(nullable=True)
    classification_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Deterministic: app aggregate band rank − expected band rank (+ = harsher)
    band_error: Mapped[int | None] = mapped_column(nullable=True)
    n_weaknesses: Mapped[int | None] = mapped_column(nullable=True)

    tokens_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="case_results")
    finding_matches: Mapped[list["FindingMatch"]] = relationship(
        back_populates="case_result", cascade="all, delete-orphan"
    )
    judge_calls: Mapped[list["JudgeCall"]] = relationship(
        back_populates="case_result", cascade="all, delete-orphan"
    )


class FindingMatch(Base):
    __tablename__ = "finding_match"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_result_id: Mapped[int] = mapped_column(
        ForeignKey("case_result.id", ondelete="CASCADE")
    )
    # matched | missed (golden not found) | extra (reported but not golden)
    match_type: Mapped[str] = mapped_column(String(16))
    expected_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_description: Mapped[str] = mapped_column(Text, default="")
    expected_severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expected_optional: Mapped[bool] = mapped_column(Boolean, default=False)
    actual_weakness_id: Mapped[int | None] = mapped_column(nullable=True)
    actual_description: Mapped[str] = mapped_column(Text, default="")
    actual_severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    counted: Mapped[bool] = mapped_column(Boolean, default=True)
    justification: Mapped[str] = mapped_column(Text, default="")

    case_result: Mapped[CaseResult] = relationship(back_populates="finding_matches")


class JudgeCall(Base):
    __tablename__ = "judge_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    case_result_id: Mapped[int] = mapped_column(
        ForeignKey("case_result.id", ondelete="CASCADE")
    )
    purpose: Mapped[str] = mapped_column(String(32))  # weakness_match | finding_class | exec_rubric
    model_id: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(16))
    latency_ms: Mapped[int | None] = mapped_column(nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    request_json: Mapped[str] = mapped_column(Text, default="")
    response_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    case_result: Mapped[CaseResult] = relationship(back_populates="judge_calls")


Index("ix_case_result_run", CaseResult.run_id)
Index("ix_case_result_case", CaseResult.case_id)
Index("ix_finding_match_cr", FindingMatch.case_result_id)
Index("ix_judge_call_cr", JudgeCall.case_result_id)
