"""ORM → API DTO converters (the simple read paths don't perfectly map via from_attributes
because of nested/JSON shapes)."""

from __future__ import annotations

from datetime import datetime, timezone
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
    StaleInfo,
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


def _stale_info(entry: dict) -> Optional[StaleInfo]:
    raw = entry.get("stale") or None
    if not raw:
        return None
    at = _parse_iso(raw.get("at")) or datetime.now(timezone.utc)
    return StaleInfo(
        at=at,
        reasons=[str(r) for r in (raw.get("reasons") or [])],
        resume_ok=bool(raw.get("resume_ok")),
    )


def compute_phase_status(a: Assessment, *, annotate: bool = True) -> dict[str, PhaseInfo]:
    """UI phases: scoping, scenarios, evidence, correlation, analysis, score
    (see app.workflow.UI_KEY for the step ↔ key mapping).

    Each phase resolves to one of pending / running / done / error by combining
    persistent timestamps in `Assessment.phase_state` with already-existing
    data signals (description sufficiency, document extraction stamps, etc.).
    A done phase may additionally carry `stale` when an upstream input changed
    after it ran. With `annotate` (the default) `ready` / `blocked_by` are
    filled from the workflow rules; the guards pass `annotate=False` because
    they only need the states.
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
                stale=_stale_info(entry),
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

    # Evidence: document-derived — upload + per-document weakness extraction.
    # Cross-correlation is its own step (below), so it no longer stands in as
    # the terminal signal here. Any document whose extraction did not complete
    # and has no live task is an error the user must retry or delete: a silent
    # gap here would let correlation run over a partial evidence bundle.
    evidence = _evidence_phase(list(a.documents))

    # Correlation: the pure cross_correlation phase.
    correlation = info_from_phase_state("cross_correlation", completed_when=False)

    # Analysis: gap_analysis phase only.
    analysis = info_from_phase_state("gap_analysis", completed_when=False)

    # Score: narratives phase only.
    score = info_from_phase_state("narratives", completed_when=False)

    phases = {
        "scoping": scoping,
        "scenarios": scenarios,
        "evidence": evidence,
        "correlation": correlation,
        "analysis": analysis,
        "score": score,
    }
    if annotate:
        from app import workflow  # local: workflow imports this module lazily too

        workflow.annotate_readiness(a, phases)
    return phases


def _evidence_phase(docs: list[Document]) -> PhaseInfo:
    from app.workflow import document_extraction_state

    if not docs:
        return PhaseInfo(state="pending")
    states = [(d, *document_extraction_state(d)) for d in docs]
    done = [d for d, s, _ in states if s == "done"]
    running = [d for d, s, _ in states if s in ("running", "pending")]
    failed = [d for d, s, _ in states if s == "error"]
    if running:
        live = next((d.weakness_task_id for d in running if d.weakness_task_id), None)
        detail, progress = _live_detail(live)
        return PhaseInfo(
            state="running",
            task_id=live,
            detail=f"{len(done)} of {len(docs)} documents extracted"
            + (f" · {detail}" if detail else ""),
            progress=len(done) / len(docs),
        )
    if failed:
        names = [d.filename for d in failed]
        return PhaseInfo(
            state="error",
            error=f"Extraction incomplete for {len(failed)} of {len(docs)} document(s) — retry or delete them.",
            failed_targets=names,
        )
    completed = [d.weakness_extracted_at for d in done if d.weakness_extracted_at]
    return PhaseInfo(state="done", completed_at=max(completed) if completed else None)


def serialize_assessment(a: Assessment) -> AssessmentRead:
    from app.workflow import current_step

    phases = compute_phase_status(a)
    return AssessmentRead(
        id=a.id,
        vendor_name=a.vendor_name,
        status=a.status,
        # Derived from the phases, not the legacy column: the first step that
        # is not done-and-current (or "report" when everything is).
        current_phase=current_step(phases),
        force_continued=a.force_continued,
        model_overrides=a.model_overrides or {},
        created_at=a.created_at,
        updated_at=a.updated_at,
        as_of_date=analysis_date(a),
        as_of_date_set=bool(a.as_of_date),
        standards_profile=standards_profile(a),
        phases=phases,
    )


def serialize_document(d: Document) -> DocumentRead:
    from app.workflow import document_extraction_state

    out = DocumentRead.model_validate(d)
    state, error = document_extraction_state(d)
    out.extraction_state = state
    out.weakness_error = error
    return out
