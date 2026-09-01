"""ORM → API DTO converters (the simple read paths don't perfectly map via from_attributes
because of nested/JSON shapes)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

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
    PhaseInfo,
    ScenarioRead,
    TurnRead,
)
from app.ai.context import analysis_date, standards_profile
from app.tasks import registry


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
        unresolved_citations=list(ca.unresolved_citations or []),
        last_error=ca.last_error,
        last_run_at=ca.last_run_at,
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
        origin_weakness_ids=list(s.origin_weakness_ids or []),
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


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Keep the datetime timezone-aware: a naive result serializes without
        # an offset and browsers then misread it as local time.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _phase_entry(state: dict, key: str) -> dict:
    return state.get(key) or {}


def _live_detail(task_id: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    if not task_id:
        return None, None
    handle = registry.get(task_id)
    if handle is None:
        return None, None
    return (handle.detail or None), handle.progress


def compute_phase_status(a: Assessment) -> dict[str, PhaseInfo]:
    """Five UI phases: scoping, scenarios, evidence, analysis, score.

    Each phase resolves to one of pending / running / done / error by combining
    persistent timestamps in `Assessment.phase_state` with already-existing
    data signals (description sufficiency, document parse timestamps, etc.).
    """
    state: dict = a.phase_state or {}

    def info_from_phase_state(phase_key: str, *, completed_when: bool) -> PhaseInfo:
        entry = _phase_entry(state, phase_key)
        started_at = _parse_iso(entry.get("started_at"))
        completed_at = _parse_iso(entry.get("completed_at"))
        task_id = entry.get("task_id")
        error = entry.get("error")
        # A live in-flight task wins over data-derived "done": partial rows
        # persisted mid-run (e.g. scenario skeletons at 20%) must not flip the
        # phase to done while the run is still writing.
        if task_id:
            handle = registry.get(task_id)
            if handle is None:
                return PhaseInfo(
                    state="error",
                    started_at=started_at,
                    error="Task lost on server restart — re-run the step.",
                )
            if handle.status in ("pending", "running"):
                return PhaseInfo(
                    state="running",
                    started_at=started_at,
                    task_id=task_id,
                    detail=handle.detail or None,
                    progress=handle.progress,
                )
            if handle.status == "error":
                return PhaseInfo(
                    state="error",
                    started_at=started_at,
                    error=handle.error or handle.detail or "Task failed",
                )
            # handle.status == "done" but mark_phase_done hasn't committed yet:
            # fall through to the completed checks below.
        if completed_when or completed_at is not None:
            return PhaseInfo(
                state="done",
                started_at=started_at,
                completed_at=completed_at,
                error=None,
                warning=entry.get("warning") or None,
                failed_targets=list(entry.get("failed_targets") or []),
            )
        if error:
            return PhaseInfo(state="error", started_at=started_at, error=error)
        return PhaseInfo(state="pending")

    # Scoping: data-derived. Done iff description.is_sufficient OR force_continued.
    desc = a.description
    scoping_done = bool((desc and desc.is_sufficient) or a.force_continued)
    scoping = PhaseInfo(state="done" if scoping_done else "pending")

    # Scenarios: tied to scenarios_generation phase.
    scenarios_done_data = bool(a.scenarios)
    scenarios = info_from_phase_state(
        "scenarios_generation", completed_when=scenarios_done_data
    )

    # Evidence: cross_correlation acts as the terminal signal — if it completed,
    # the per-document extractions that fed it are by definition complete and
    # the phase is done. If cross_correlation hasn't run yet, fall back to
    # showing live extraction progress.
    docs = list(a.documents)
    cc_entry = _phase_entry(state, "cross_correlation")
    cc_completed_at = _parse_iso(cc_entry.get("completed_at"))
    cc_task = cc_entry.get("task_id")
    cc_error = cc_entry.get("error")
    if cc_completed_at is not None:
        evidence = PhaseInfo(state="done", completed_at=cc_completed_at)
    elif cc_task:
        handle = registry.get(cc_task)
        if handle is None:
            evidence = PhaseInfo(
                state="error",
                error="Task lost on server restart — re-run the step.",
            )
        else:
            evidence = PhaseInfo(
                state="running",
                started_at=_parse_iso(cc_entry.get("started_at")),
                task_id=cc_task,
                detail=handle.detail or None,
                progress=handle.progress,
            )
    elif cc_error:
        evidence = PhaseInfo(state="error", error=cc_error)
    elif docs:
        done_n = sum(1 for d in docs if d.weakness_extracted_at is not None)
        if done_n < len(docs):
            evidence = PhaseInfo(
                state="running",
                detail=f"{done_n} of {len(docs)} documents extracted",
                progress=done_n / len(docs),
            )
        else:
            # All extracted but cross-correlation never fired (e.g. user uploaded
            # docs while the auto-fire path is between extraction and correlation,
            # or the manual synthesise endpoint is the next click).
            evidence = PhaseInfo(state="pending")
    else:
        evidence = PhaseInfo(state="pending")

    # Correlation: the pure cross_correlation phase, exposed as its own key so
    # the analysis page's "cross-correlate" sub-step never shows per-document
    # extraction progress (the composite "evidence" key above folds both in
    # for the nav).
    correlation = info_from_phase_state("cross_correlation", completed_when=False)

    # Analysis: gap_analysis phase only.
    analysis = info_from_phase_state("gap_analysis", completed_when=False)

    # Score: narratives phase only.
    score = info_from_phase_state("narratives", completed_when=False)

    return {
        "scoping": scoping,
        "scenarios": scenarios,
        "evidence": evidence,
        "correlation": correlation,
        "analysis": analysis,
        "score": score,
    }


def serialize_assessment(a: Assessment) -> AssessmentRead:
    return AssessmentRead(
        id=a.id,
        vendor_name=a.vendor_name,
        status=a.status,
        current_phase=a.current_phase,
        force_continued=a.force_continued,
        model_overrides=a.model_overrides or {},
        created_at=a.created_at,
        updated_at=a.updated_at,
        as_of_date=analysis_date(a),
        as_of_date_set=bool(a.as_of_date),
        standards_profile=standards_profile(a),
        phases=compute_phase_status(a),
    )


def serialize_document(d: Document) -> DocumentRead:
    return DocumentRead.model_validate(d)
