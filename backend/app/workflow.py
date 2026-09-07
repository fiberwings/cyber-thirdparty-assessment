"""Single source of truth for the assessment workflow.

The pipeline is a strict chain — every step consumes the persisted output of
the one before it:

    scoping → scenarios → evidence → correlation → analysis → narratives

* scoping      description + sufficiency Q&A (or force-continue)
* scenarios    inherent-risk scenarios + expected controls
* evidence     document upload + per-document weakness extraction
* correlation  attestation checks, candidate confirmation, mapping of
               weaknesses onto scenario controls, emergent scenarios
* analysis     gap analysis over every scenario × control (reads the mapped,
               confirmed weaknesses so it does not re-emit them)
* narratives   per-scenario prose + executive summary over the scored state

Three things are enforced here and nowhere else:

1. **Prerequisites** — a step may start only when every upstream step is
   done and not stale (`require_step_ready`, 409).
2. **Mutual exclusion** — one AI job per assessment; a second request for the
   *same* step re-attaches to the run in flight, anything else is refused
   (`require_no_run_in_flight`, 409). Input edits are refused while a job
   runs, because the job may be writing the rows being edited.
3. **Invalidation** — when an input changes, every downstream step that had
   completed is stamped `stale` (kept visible, with the reason) and blocks
   further progression until re-run (`invalidate_downstream`). Stamps are
   written by the mutator, never inferred.

`compute_phase_status` (app.api.serializers) exposes `stale`, `ready` and
`blocked_by` per phase so the frontend renders the rules without duplicating
them. Sync mutators run in the threadpool while job submits run on the event
loop; the check-then-act window between them is theoretical for the
single-user deployment and is accepted.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Iterable

from fastapi import HTTPException

from app.schemas.api import BlockReason, PhaseInfo, TaskStatusRead, WorkflowConflict
from app.tasks import TaskHandle, registry

if TYPE_CHECKING:
    from app.models import Assessment, Document

# ---------- Steps ----------

STEPS: tuple[str, ...] = (
    "scoping",
    "scenarios",
    "evidence",
    "correlation",
    "analysis",
    "narratives",
)

# Step → Assessment.phase_state key. Steps without a key are derived from
# data (description sufficiency, document extraction stamps) and are never
# stamped stale — their state simply reflects the data.
PHASE_KEY: dict[str, str | None] = {
    "scoping": None,
    "scenarios": "scenarios_generation",
    "evidence": None,
    "correlation": "cross_correlation",
    "analysis": "gap_analysis",
    "narratives": "narratives",
}

# Step → key in AssessmentRead.phases.
UI_KEY: dict[str, str] = {
    "scoping": "scoping",
    "scenarios": "scenarios",
    "evidence": "evidence",
    "correlation": "correlation",
    "analysis": "analysis",
    "narratives": "score",
}

STEP_LABEL: dict[str, str] = {
    "scoping": "Scoping",
    "scenarios": "Scenario generation",
    "evidence": "Evidence extraction",
    "correlation": "Cross-correlation",
    "analysis": "Gap analysis",
    "narratives": "Narratives & summary",
}

# Canonical task kinds (TaskHandle.kind / TaskRecord.kind).
KIND_SCOPING_TURN = "scoping_turn"
KIND_SCENARIOS = "scenarios_generation"
KIND_EXTRACTION = "document_extraction"
KIND_CORRELATION = "cross_correlation"
KIND_GAP = "gap_analysis"
KIND_GAP_CONTROL = "gap_analysis_control"
KIND_NARRATIVES = "narratives"
KIND_EXEC_SUMMARY = "executive_summary"

KIND_LABEL: dict[str, str] = {
    KIND_SCOPING_TURN: "Scoping",
    KIND_SCENARIOS: "Scenario generation",
    KIND_EXTRACTION: "Document extraction",
    KIND_CORRELATION: "Cross-correlation",
    KIND_GAP: "Gap analysis",
    KIND_GAP_CONTROL: "Per-control gap analysis",
    KIND_NARRATIVES: "Narratives & summary",
    KIND_EXEC_SUMMARY: "Executive summary",
}

# Legacy `Assessment.current_phase` vocabulary (also what the left nav keys
# on). Used by the one-off phase_state backfill in app.db and by
# `current_step` below.
CURRENT_PHASE_FOR_STEP: dict[str, str] = {
    "scoping": "scoping",
    "scenarios": "scenarios",
    "evidence": "evidence",
    "correlation": "analysis",
    "analysis": "analysis",
    "narratives": "score",
}
LEGACY_PHASES_BY_CURRENT: dict[str, list[str]] = {
    "scoping": [],
    "scenarios": ["scoping"],
    "evidence": ["scoping", "scenarios_generation"],
    "analysis": ["scoping", "scenarios_generation", "cross_correlation", "gap_analysis"],
    "score": ["scoping", "scenarios_generation", "cross_correlation", "gap_analysis", "narratives"],
    "report": ["scoping", "scenarios_generation", "cross_correlation", "gap_analysis", "narratives"],
}


def _index(step: str) -> int:
    try:
        return STEPS.index(step)
    except ValueError:
        raise KeyError(f"Unknown workflow step {step!r}") from None


def upstream(step: str) -> tuple[str, ...]:
    return STEPS[: _index(step)]


def downstream(step: str) -> tuple[str, ...]:
    """Steps strictly after `step`."""
    return STEPS[_index(step) + 1 :]


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ---------- Stale stamps ----------


def stale_entry(a: "Assessment", step: str) -> dict | None:
    key = PHASE_KEY.get(step)
    if key is None:
        return None
    return ((a.phase_state or {}).get(key) or {}).get("stale") or None


def invalidate_downstream(
    a: "Assessment", from_step: str, *, reason: str, resume_ok: bool = False
) -> list[str]:
    """Stamp `from_step` and every later step that has completed as stale.

    The caller commits. Steps that are pending / errored have no artefact to
    distrust and are left alone — the prerequisite guard blocks them anyway
    because the stamped step upstream is no longer "done and current".

    Merge rule when a step is already stale: keep the earliest `at`, append
    the new reason (deduped, capped), and AND the `resume_ok` flags — a
    resume-safe change followed by a full-rerun change requires a full rerun.
    """
    from app.api.serializers import compute_phase_status  # local: avoids a cycle

    phases = compute_phase_status(a, annotate=False)
    state = dict(a.phase_state or {})
    stamped: list[str] = []
    for step in (from_step, *downstream(from_step)):
        key = PHASE_KEY.get(step)
        if key is None:
            continue
        if phases[UI_KEY[step]].state != "done":
            continue
        entry = dict(state.get(key) or {})
        previous = entry.get("stale") or None
        reasons = list((previous or {}).get("reasons") or [])
        if reason not in reasons:
            reasons.append(reason)
        entry["stale"] = {
            "at": (previous or {}).get("at") or _now_iso(),
            "reasons": reasons[-5:],
            "resume_ok": bool(resume_ok) and (previous is None or bool(previous.get("resume_ok"))),
        }
        state[key] = entry
        stamped.append(step)
    if stamped:
        a.phase_state = state
    return stamped


# ---------- Per-document extraction state ----------


def document_extraction_state(doc: "Document") -> tuple[str, str | None]:
    """(state, error) for one document's weakness extraction.

    done     — `weakness_extracted_at` stamped
    running  — a live task owns it
    error    — failed, interrupted, or never ran (legacy row); the message
               says which, and the evidence page offers a retry
    pending  — not parsed yet (upload still in progress)
    """
    if doc.weakness_extracted_at is not None:
        return "done", None
    if doc.weakness_task_id:
        handle = registry.get(doc.weakness_task_id)
        if handle is not None and handle.status in ("pending", "running"):
            return "running", None
        if handle is None:
            return "error", doc.weakness_error or "Extraction task lost on server restart — retry extraction"
        return "error", doc.weakness_error or handle.error or handle.detail or "Extraction failed — retry"
    if doc.weakness_error:
        return "error", doc.weakness_error
    if doc.parsed_at is None:
        return "pending", None
    return "error", "Extraction never ran for this document — retry extraction"


# ---------- Rules ----------


def _block(code: str, message: str, **extra) -> dict:
    return BlockReason(code=code, message=message, **extra).model_dump()


def _evidence_blockers(a: "Assessment", info: PhaseInfo) -> list[dict]:
    docs = list(a.documents)
    if not docs:
        return [_block("no_documents", "Upload at least one evidence document first.", step="evidence")]
    running = [d for d in docs if document_extraction_state(d)[0] == "running"]
    failed = [d for d in docs if document_extraction_state(d)[0] == "error"]
    out: list[dict] = []
    if running:
        out.append(
            _block(
                "extraction_incomplete",
                f"{len(running)} of {len(docs)} document(s) still extracting — wait for extraction to finish.",
                step="evidence",
                document_ids=[d.id for d in running],
            )
        )
    if failed:
        names = ", ".join(d.filename for d in failed[:4]) + ("…" if len(failed) > 4 else "")
        out.append(
            _block(
                "extraction_failed",
                f"Extraction failed for {names} — retry it from the evidence page (or delete the document).",
                step="evidence",
                document_ids=[d.id for d in failed],
            )
        )
    if not out and info.state != "done":
        out.append(_block("prerequisite_pending", "Evidence extraction has not completed.", step="evidence"))
    return out


def _upstream_blocker(a: "Assessment", step: str, phases: dict[str, PhaseInfo]) -> list[dict]:
    """The first upstream step that is not done-and-current, as blockers.

    Only the earliest failing step is reported: it is the one the user must
    address first, and everything after it is blocked by it anyway.
    """
    for up in upstream(step):
        info = phases[UI_KEY[up]]
        if info.state == "done" and info.stale is None:
            continue
        label = STEP_LABEL[up]
        if up == "scoping":
            if a.description is None or not (a.description.text or "").strip():
                return [_block("missing_description", "Set the service description first.", step="scoping")]
            return [_block("prerequisite_pending", "Finish scoping first (answer the questions or force-continue).", step="scoping")]
        if up == "evidence":
            return _evidence_blockers(a, info)
        if info.state == "running":
            return [_block("prerequisite_running", f"{label} is still running — wait for it to finish.", step=up, task_id=info.task_id)]
        if info.state == "error":
            return [_block("prerequisite_error", f"{label} failed — re-run it first.", step=up)]
        if info.state == "done" and info.stale is not None:
            why = "; ".join(info.stale.reasons) or "inputs changed"
            return [_block("prerequisite_stale", f"{label} is stale ({why}) — re-run it first.", step=up)]
        return [_block("prerequisite_pending", f"Run {label.lower()} first.", step=up)]
    return []


def blockers_for(
    a: "Assessment", step: str, phases: dict[str, PhaseInfo], *, resume: bool = False
) -> list[dict]:
    """Pure rule function: why `step` cannot start now (empty = it can).

    `resume=True` is the `only_failed` / per-control gap-analysis path: it
    needs a previous attempt to resume and refuses when the inputs changed in
    a way a partial re-run cannot fix.
    """
    _index(step)
    blockers = _upstream_blocker(a, step, phases)
    if blockers:
        return blockers
    if step == "scoping":
        if a.description is None or not (a.description.text or "").strip():
            return [_block("missing_description", "Set the service description first.", step="scoping")]
    if step == "analysis" and resume:
        entry = (a.phase_state or {}).get(PHASE_KEY["analysis"]) or {}
        if not entry.get("started_at"):
            return [_block("resume_requires_full_run", "No gap analysis run to resume — run the full gap analysis.", step="analysis")]
        stale = entry.get("stale") or None
        if stale and not stale.get("resume_ok"):
            why = "; ".join(stale.get("reasons") or []) or "inputs changed"
            return [_block("resume_requires_full_run", f"Inputs changed since the last gap analysis ({why}) — run the full gap analysis.", step="analysis")]
    return []


# ---------- Guards ----------


def _conflict(step: str, blockers: list[dict]) -> HTTPException:
    body = WorkflowConflict(
        code=blockers[0]["code"],
        step=step,
        missing=[BlockReason(**b) for b in blockers],
        message=" ".join(b["message"] for b in blockers),
    )
    return HTTPException(status_code=409, detail=body.model_dump())


def require_step_ready(
    a: "Assessment", step: str, *, phases: dict[str, PhaseInfo] | None = None, resume: bool = False
) -> None:
    """409 unless every upstream step is done and current (and the step's own
    conditions hold)."""
    if phases is None:
        from app.api.serializers import compute_phase_status  # local: avoids a cycle

        phases = compute_phase_status(a, annotate=False)
    blockers = blockers_for(a, step, phases, resume=resume)
    if blockers:
        raise _conflict(step, blockers)


def in_flight(a: "Assessment") -> list[TaskHandle]:
    """Live jobs for this assessment: the registry's in-memory view plus any
    `task` row still pending/running (durable state written through by the
    registry, or seeded outside this process — e.g. in tests). Rows left by a
    previous process are reconciled to `error` at startup, so a durable
    pending/running row is always a genuine live job."""
    seen: dict[str, TaskHandle] = {t.id: t for t in registry.active(a.id)}
    try:
        from app.db import SessionLocal
        from app.models import TaskRecord

        with SessionLocal() as db:
            rows = (
                db.query(TaskRecord)
                .filter(TaskRecord.assessment_id == a.id, TaskRecord.status.in_(("pending", "running")))
                .all()
            )
            for row in rows:
                if row.id in seen:
                    continue
                handle = registry.get(row.id)
                if handle is not None and handle.status in ("pending", "running"):
                    seen[row.id] = handle
    except Exception:
        pass
    return list(seen.values())


def require_no_run_in_flight(
    a: "Assessment",
    *,
    allow_kinds: Iterable[str] = (),
    reattach_kind: str | None = None,
) -> TaskHandle | None:
    """Assessment-level mutual exclusion.

    Returns the live handle when a job of `reattach_kind` is already running
    (the caller answers 200 with it instead of starting a second run); raises
    409 `run_in_flight` for any other live job not in `allow_kinds`.
    """
    allowed = set(allow_kinds)
    reattach: TaskHandle | None = None
    for t in in_flight(a):
        if t.kind in allowed:
            continue
        if reattach_kind is not None and t.kind == reattach_kind:
            reattach = reattach or t
            continue
        label = KIND_LABEL.get(t.kind, t.kind or "A job")
        raise _conflict(
            reattach_kind or "edit",
            [_block("run_in_flight", f"{label} is running — wait for it to finish.", task_id=t.id, kind=t.kind)],
        )
    return reattach


def reattach_response(handle: TaskHandle) -> TaskStatusRead:
    from app.tasks import task_status_read

    return task_status_read(handle)


# ---------- Readiness annotation for the API ----------

# Jobs that do not block a step's action (uploads may proceed while another
# document extracts — they are the same step).
_ALLOWED_WHILE_RUNNING: dict[str, tuple[str, ...]] = {"evidence": (KIND_EXTRACTION,)}


def annotate_readiness(a: "Assessment", phases: dict[str, PhaseInfo]) -> None:
    """Fill PhaseInfo.ready / blocked_by using the same rules as the guards."""
    live = in_flight(a)
    for step in STEPS:
        info = phases[UI_KEY[step]]
        blockers = blockers_for(a, step, phases)
        if not blockers:
            allowed = set(_ALLOWED_WHILE_RUNNING.get(step, ()))
            for t in live:
                if t.kind in allowed:
                    continue
                label = KIND_LABEL.get(t.kind, t.kind or "A job")
                blockers.append(
                    _block("run_in_flight", f"{label} is running — wait for it to finish.", task_id=t.id, kind=t.kind)
                )
                break
        info.blocked_by = [BlockReason(**b) for b in blockers]
        info.ready = not blockers


def current_step(phases: dict[str, PhaseInfo]) -> str:
    """Legacy `current_phase` value: the first step that is not done and
    current, or "report" when everything is."""
    for step in STEPS:
        info = phases[UI_KEY[step]]
        if not (info.state == "done" and info.stale is None):
            return CURRENT_PHASE_FOR_STEP[step]
    return "report"
