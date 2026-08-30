"""Assessment-level context shared by every prompt (R7).

Two inputs the model must never guess:
- the analysis date (`Assessment.as_of_date`, default today) — every
  freshness / staleness judgement is made relative to it;
- the assessor standards profile — the client's own requirements.

`assessment_context_block()` renders both as a header for user messages;
`analysis_date()` gives the date for deterministic date arithmetic.
"""

from __future__ import annotations

from datetime import date, datetime

from app.models import Assessment
from app.schemas.standards import StandardsProfile


def analysis_date(assessment: Assessment | None) -> date:
    raw = getattr(assessment, "as_of_date", None) if assessment is not None else None
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.utcnow().date()


def analysis_datetime(assessment: Assessment | None) -> datetime:
    d = analysis_date(assessment)
    return datetime(d.year, d.month, d.day)


def standards_profile(assessment: Assessment | None) -> StandardsProfile:
    raw = getattr(assessment, "standards_profile", None) if assessment is not None else None
    try:
        return StandardsProfile.model_validate(raw or {})
    except Exception:
        return StandardsProfile()


def analysis_date_line(assessment: Assessment | None) -> str:
    return f"# Analysis date: {analysis_date(assessment).isoformat()}"


def standards_block(assessment: Assessment | None) -> str:
    return "# Assessor standards (the client's own requirements)\n" + standards_profile(assessment).render()


def assessment_context_block(assessment: Assessment | None) -> str:
    """Header for user messages: analysis date + assessor standards."""
    return f"{analysis_date_line(assessment)}\n{standards_block(assessment)}"
