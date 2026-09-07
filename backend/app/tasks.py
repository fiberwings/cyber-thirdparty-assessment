"""Task registry for long-running pipeline runs.

Single-process, single-user deployment — a dict + asyncio drives the live
side (progress events, SSE). Every status change is also written through to
the `task` table so `GET /api/tasks/{id}` keeps answering after a server
restart: tasks that were pending/running when the process died are marked
`error` ("interrupted by server restart") at startup, and the phase that
owned them is marked failed so the UI offers a re-run instead of hanging.

Liveness: every handle carries `last_activity_at`, advanced by progress
updates and — through `app.activity` — by every streamed token/keepalive the
LLM router receives on behalf of the job. A watchdog (`watchdog_loop`,
started in the app lifespan) cancels tasks that show no activity for
TASK_IDLE_TIMEOUT_S or run longer than TASK_MAX_RUNTIME_S, and users can
cancel via POST /api/tasks/{id}/cancel. Cancellation aborts any in-flight
model stream and fails the owned state exactly like a restart would, so the
assessment is never wedged behind a dead job.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from app import activity
from app.config import settings

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

    from app.models import TaskRecord
    from app.schemas.api import TaskStatusRead

JobFn = Callable[["TaskHandle"], Awaitable[Any]]

_logger = logging.getLogger(__name__)
_FINISHED_HANDLE_TTL_S = 3600.0


@dataclass
class TaskEvent:
    ts: float
    event: str
    data: dict


@dataclass
class TaskStats:
    """Structured, honest progress for the UI. Everything here is observed,
    never estimated for display purposes except the token counters while a
    stream is still open (chars // 4, reconciled to the provider's exact
    usage when the attempt ends). `units_total == 0` means the current stage
    has no knowable unit count and the UI must not draw a bar."""

    stage: str = ""              # current stage label ("Confirming candidates")
    stage_index: int = -1        # index into `stages`; -1 before the first stage
    stages: list[str] = field(default_factory=list)  # planned order, may grow lazily
    units_done: int = 0
    units_total: int = 0
    unit_label: str = ""         # "controls" | "documents" | "windows" | ...
    purpose: str = ""            # router purpose of the most recent LLM call
    calls_active: int = 0
    calls_done: int = 0
    tokens_out: int = 0          # cumulative output tokens across the task
    tokens_reasoning: int = 0    # cumulative reasoning tokens (0 when hidden)
    first_token_ms: int | None = None  # TTFT of the most recent attempt


@dataclass
class TaskHandle:
    id: str
    status: str = "pending"  # pending|running|done|error
    progress: float = 0.0
    detail: str = ""
    error: str = ""
    result: Any = None
    kind: str = ""
    assessment_id: int | None = None
    events: list[TaskEvent] = field(default_factory=list)
    # Liveness. Monotonic stamps drive the watchdog; wall-clock stamps are
    # what the API reports. `detached` marks a snapshot of another process's
    # task (no asyncio task to cancel, no events).
    created_mono: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=datetime.utcnow)
    last_activity_mono: float = field(default_factory=time.monotonic)
    last_activity_at: datetime = field(default_factory=datetime.utcnow)
    detached: bool = False
    stats: TaskStats = field(default_factory=TaskStats)
    _cv: asyncio.Condition = field(default_factory=asyncio.Condition)
    # Per-attempt token estimates (chars of streamed output); folded into
    # the committed counters when the attempt ends.
    _tokens_committed: int = 0
    _reasoning_committed: int = 0
    _chars_out: int = 0
    _chars_reasoning: int = 0
    _last_activity_persist: float = 0.0
    _task: asyncio.Task | None = None
    _cancel_reason: str | None = None

    @property
    def idle_s(self) -> float:
        """Seconds since the last activity ping (wall clock, so it is also
        meaningful for a detached snapshot)."""
        return max(0.0, (datetime.utcnow() - self.last_activity_at).total_seconds())

    @property
    def live(self) -> bool:
        return self.status in ("pending", "running")

    @property
    def elapsed_s(self) -> float:
        """Wall-clock seconds the task has run: to now while live, to the
        last activity stamp (the done/error write) once finished."""
        end = datetime.utcnow() if self.live else self.last_activity_at
        return max(0.0, (end - self.started_at).total_seconds())

    # ---- structured progress (sync, best effort) ----

    def stats_dict(self) -> dict:
        d = asdict(self.stats)
        d["tokens_out"] = self._tokens_committed + self._chars_out // 4
        d["tokens_reasoning"] = self._reasoning_committed + self._chars_reasoning // 4
        return d

    def plan(self, stages: list[str]) -> None:
        """Declare the stages the job will run through, in order."""
        self.stats.stages = list(stages)
        self.stats.stage_index = -1
        self.set(event="plan")

    def stage(self, label: str, *, units_total: int = 0, unit_label: str = "",
              detail: str | None = None) -> None:
        """Enter a stage. Unknown labels are appended to the plan (jobs whose
        path is decided at runtime plan lazily). Resets the unit counter."""
        st = self.stats
        if label not in st.stages:
            st.stages.append(label)
        st.stage = label
        st.stage_index = st.stages.index(label)
        st.units_done = 0
        st.units_total = max(0, int(units_total or 0))
        st.unit_label = unit_label if st.units_total else ""
        self._derive_progress()
        self.set(detail=detail if detail is not None else label, event="stage")

    def advance(self, done: int | None = None, *, detail: str | None = None) -> None:
        """One more unit of the current stage done (or set the absolute count)."""
        st = self.stats
        st.units_done = int(done) if done is not None else st.units_done + 1
        if st.units_total:
            st.units_done = min(st.units_done, st.units_total)
        self._derive_progress()
        self.set(detail=detail, event="progress")

    def _derive_progress(self) -> None:
        """Monotonic progress from stages/units; never a guess. Stays below
        1.0 until the registry marks the task done."""
        st = self.stats
        frac = (st.units_done / st.units_total) if st.units_total else 0.0
        if st.stages and st.stage_index >= 0:
            derived = (st.stage_index + frac) / len(st.stages)
        elif st.units_total:
            derived = frac
        else:
            return
        self.progress = max(self.progress, min(0.99, derived))

    # Hot-path hooks from the LLM router (in-memory only; the row follows on
    # the touch() throttle). Never raise — see app.activity.
    def note_call_started(self, purpose: str) -> None:
        self.stats.calls_active += 1
        if purpose:
            self.stats.purpose = purpose

    def note_call_finished(self) -> None:
        self.stats.calls_active = max(0, self.stats.calls_active - 1)
        self.stats.calls_done += 1

    def note_delta(self, n_chars: int, *, reasoning: bool = False) -> None:
        if reasoning:
            self._chars_reasoning += max(0, int(n_chars))
        else:
            self._chars_out += max(0, int(n_chars))

    def note_first_token(self, ms: int | None) -> None:
        if ms is not None:
            self.stats.first_token_ms = int(ms)

    def note_attempt_finished(self, completion_tokens: int | None = None,
                              reasoning_tokens: int | None = None) -> None:
        """Fold the running estimate of one streamed attempt into the
        committed counters — exact when the provider's usage chunk arrived,
        the chars//4 estimate otherwise (a failed or cut-off attempt still
        spent those tokens)."""
        self._tokens_committed += (
            int(completion_tokens) if completion_tokens is not None else self._chars_out // 4
        )
        self._reasoning_committed += (
            int(reasoning_tokens) if reasoning_tokens is not None else self._chars_reasoning // 4
        )
        self._chars_out = 0
        self._chars_reasoning = 0

    def touch(self) -> None:
        """Record activity (a streamed token, a keepalive, a progress step).
        Hot path: in-memory always; the row at most every
        TASK_ACTIVITY_PERSIST_S so a token stream does not become a write
        stream."""
        now = time.monotonic()
        self.last_activity_mono = now
        self.last_activity_at = datetime.utcnow()
        if now - self._last_activity_persist >= settings.task_activity_persist_s:
            self._last_activity_persist = now
            self._persist_activity()

    def _persist_activity(self) -> None:
        try:
            from app.db import SessionLocal
            from app.models import TaskRecord

            with SessionLocal() as db:
                db.query(TaskRecord).filter(TaskRecord.id == self.id).update(
                    {"last_activity_at": self.last_activity_at, "stats": self.stats_dict()}
                )
                db.commit()
        except Exception:
            pass

    def _persist(self) -> None:
        """Write-through of the durable fields. Best effort: a DB hiccup must
        never break the running job."""
        try:
            from app.db import SessionLocal
            from app.models import TaskRecord

            with SessionLocal() as db:
                row = db.get(TaskRecord, self.id)
                if row is None:
                    row = TaskRecord(id=self.id, kind=self.kind, assessment_id=self.assessment_id)
                    db.add(row)
                row.status = self.status
                row.progress = float(self.progress or 0.0)
                row.detail = (self.detail or "")[:2000]
                row.error = (self.error or "")[:2000]
                row.last_activity_at = self.last_activity_at
                row.stats = self.stats_dict()
                db.commit()
        except Exception:
            pass

    def set(self, *, status: str | None = None, progress: float | None = None,
            detail: str | None = None, event: str = "progress",
            data: dict | None = None) -> None:
        """Synchronous progress update — safe to call from anywhere."""
        # A progress update is activity; the write-through below persists
        # the stamp, so skip the throttled activity write.
        self.last_activity_mono = time.monotonic()
        self.last_activity_at = datetime.utcnow()
        self._last_activity_persist = self.last_activity_mono
        if status is not None:
            self.status = status
        if progress is not None:
            self.progress = progress
        if detail is not None:
            self.detail = detail
        ev = TaskEvent(
            ts=time.time(),
            event=event,
            data={"status": self.status, "progress": self.progress, "detail": self.detail, **(data or {})},
        )
        self.events.append(ev)
        self._persist()

    async def update(self, *, status: str | None = None, progress: float | None = None,
                     detail: str | None = None, event: str = "progress",
                     data: dict | None = None):
        """Async update — also notifies any SSE subscribers."""
        self.set(status=status, progress=progress, detail=detail, event=event, data=data)
        async with self._cv:
            self._cv.notify_all()


class TaskRegistry:
    def __init__(self):
        self._tasks: dict[str, TaskHandle] = {}

    def get(self, task_id: str) -> TaskHandle | None:
        """Live handle when the task belongs to this process; otherwise a
        detached snapshot from the `task` table (status/progress/detail/error
        only — no events, no result)."""
        live = self._tasks.get(task_id)
        if live is not None:
            return live
        try:
            from app.db import SessionLocal
            from app.models import TaskRecord

            with SessionLocal() as db:
                row = db.get(TaskRecord, task_id)
                if row is None:
                    return None
                stamp = row.last_activity_at or row.updated_at or row.created_at or datetime.utcnow()
                handle = TaskHandle(
                    id=row.id,
                    status=row.status,
                    progress=row.progress,
                    detail=row.detail,
                    error=row.error,
                    kind=row.kind,
                    assessment_id=row.assessment_id,
                    started_at=row.created_at or stamp,
                    last_activity_at=stamp,
                    detached=True,
                    stats=_stats_from_row(row.stats),
                )
                handle._tokens_committed = handle.stats.tokens_out
                handle._reasoning_committed = handle.stats.tokens_reasoning
                return handle
        except Exception:
            return None

    def active(self, assessment_id: int, *, exclude_kinds: tuple[str, ...] = ()) -> list[TaskHandle]:
        """Live pending/running tasks of this process for one assessment.

        The in-memory dict is the truth for the single-process deployment;
        tasks from a previous process are reconciled to `error` at startup and
        tasks of this process that go quiet are cancelled by the watchdog, so
        there is nothing durable to add here. Used by the workflow guards for
        assessment-level mutual exclusion."""
        return [
            t
            for t in self._tasks.values()
            if t.assessment_id == assessment_id
            and t.status in ("pending", "running")
            and t.kind not in exclude_kinds
        ]

    def submit(self, fn: JobFn, *, kind: str = "", assessment_id: int | None = None) -> TaskHandle:
        task = TaskHandle(id=str(uuid.uuid4()), kind=kind, assessment_id=assessment_id)
        self._tasks[task.id] = task
        task._persist()

        async def _wrap():
            # Ambient handle for the job and every coroutine it fans out to
            # (contextvars are copied into child tasks) — the LLM router pings
            # it per streamed line.
            token = activity.current_task.set(task)
            try:
                await task.update(status="running", event="start")
                try:
                    task.result = await fn(task)
                    await task.update(status="done", progress=1.0, event="done")
                except asyncio.CancelledError:
                    _mark_cancelled(task)
                    async with task._cv:
                        task._cv.notify_all()
                    # Swallowed on purpose: the job is top-level, nothing awaits
                    # it, and a clean stop is the point of cancelling.
                except Exception as e:
                    task.error = str(e)
                    await task.update(status="error", detail=str(e)[:300], event="error")
            finally:
                activity.current_task.reset(token)

        task._task = asyncio.create_task(_wrap())
        # A task cancelled before `_wrap` ever ran never reaches the handler
        # above; finish the bookkeeping from the done callback instead.
        task._task.add_done_callback(lambda fut: _finalize_cancelled(task, fut))
        return task

    def cancel(self, task_id: str, *, reason: str) -> bool:
        """Cancel a live task of this process. Returns False when the task is
        unknown, finished, or a detached snapshot of another process (nothing
        to cancel — the restart reconciliation already failed it)."""
        t = self._tasks.get(task_id)
        if t is None or t._task is None or t._task.done() or not t.live:
            return False
        t._cancel_reason = reason
        t._task.cancel()
        return True

    async def watchdog_tick(self) -> list[str]:
        """One watchdog pass: cancel live tasks idle beyond
        TASK_IDLE_TIMEOUT_S or older than TASK_MAX_RUNTIME_S; forget finished
        handles after an hour (their rows stay). Returns the cancelled ids."""
        now = time.monotonic()
        cancelled: list[str] = []
        for t in list(self._tasks.values()):
            if t.live and t._task is not None and not t._task.done():
                idle = now - t.last_activity_mono
                age = now - t.created_mono
                if idle > settings.task_idle_timeout_s:
                    reason = (
                        f"no activity for {idle:.0f}s "
                        f"(TASK_IDLE_TIMEOUT_S={settings.task_idle_timeout_s:.0f})"
                    )
                elif age > settings.task_max_runtime_s:
                    reason = (
                        f"running for {age:.0f}s "
                        f"(TASK_MAX_RUNTIME_S={settings.task_max_runtime_s:.0f})"
                    )
                else:
                    continue
                _logger.warning("watchdog cancelling task %s (%s): %s", t.id, t.kind, reason)
                if self.cancel(t.id, reason=reason):
                    cancelled.append(t.id)
            elif not t.live and now - t.last_activity_mono > _FINISHED_HANDLE_TTL_S:
                self._tasks.pop(t.id, None)
        return cancelled

    async def watchdog_loop(self) -> None:
        """Background loop for the app lifespan. Never dies on an error."""
        while True:
            await asyncio.sleep(settings.task_watchdog_interval_s)
            try:
                await self.watchdog_tick()
            except Exception:  # pragma: no cover — defensive
                _logger.exception("task watchdog tick failed")

    async def stream(self, task_id: str) -> AsyncIterator[TaskEvent]:
        task = self._tasks.get(task_id)
        if task is None:
            return
        cursor = 0
        while True:
            # Drain any pending events
            while cursor < len(task.events):
                yield task.events[cursor]
                cursor += 1
            if task.status in {"done", "error"}:
                return
            async with task._cv:
                try:
                    await asyncio.wait_for(task._cv.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    # heartbeat
                    yield TaskEvent(
                        time.time(), "heartbeat",
                        {"status": task.status, "idle_s": round(task.idle_s, 1)},
                    )


registry = TaskRegistry()


_STATS_FIELDS = tuple(f.name for f in fields(TaskStats))


def _stats_from_row(raw: object) -> TaskStats:
    data = raw if isinstance(raw, dict) else {}
    kwargs = {k: data[k] for k in _STATS_FIELDS if k in data}
    try:
        return TaskStats(**kwargs)
    except Exception:  # a row written by a newer/older schema
        return TaskStats()


def task_progress_fields(handle: TaskHandle) -> dict:
    """The structured-progress block shared by TaskStatusRead and PhaseInfo
    (see TaskProgressFields): the stats plus wall-clock elapsed seconds."""
    out = handle.stats_dict()
    out["elapsed_s"] = round(handle.elapsed_s, 1)
    return out


def task_status_read(handle: TaskHandle) -> "TaskStatusRead":
    """The one place a task status payload is built (poll endpoint, submit
    responses, re-attach). `idle_s` only while the task is live."""
    from app.schemas.api import TaskStatusRead

    return TaskStatusRead(
        task_id=handle.id,
        status=handle.status,
        progress=handle.progress,
        detail=handle.detail or handle.error,
        kind=handle.kind,
        error=handle.error or "",
        started_at=handle.started_at,
        last_activity_at=handle.last_activity_at,
        idle_s=round(handle.idle_s, 1) if handle.live else None,
        **task_progress_fields(handle),
    )


def _mark_cancelled(task: TaskHandle) -> None:
    """Cancellation bookkeeping (sync — safe after the CancelledError): the
    handle, its row, and the state the job owned (documents, phase entry)."""
    if not task.live:
        return
    reason = task._cancel_reason or "server shutdown"
    task.error = f"cancelled: {reason}"
    task.set(status="error", detail=task.error[:300], event="cancelled")
    try:
        from app.db import SessionLocal
        from app.models import TaskRecord

        with SessionLocal() as db:
            row = db.get(TaskRecord, task.id)
            if row is not None:
                _fail_owned_state(db, row, task.error, doc_error=f"{task.error} — retry extraction")
                db.commit()
    except Exception:  # best effort, like _persist
        _logger.exception("could not fail owned state for cancelled task %s", task.id)


def _finalize_cancelled(task: TaskHandle, fut: "asyncio.Future[Any]") -> None:
    if fut.cancelled() and task.live:
        _mark_cancelled(task)


def _fail_owned_state(db: "Session", row: "TaskRecord", error: str, *, doc_error: str) -> None:
    """Fail one task row and everything it owned: documents whose extraction
    it was (retryable error, never a silent gap) and the phase_state entry
    that points at it (so the UI offers a re-run). Shared by the restart
    reconciliation and the cancel path. Caller commits."""
    from app.models import Assessment, Document

    row.status = "error"
    row.error = error[:2000]
    row.detail = row.error
    for doc in db.query(Document).filter(Document.weakness_task_id == row.id).all():
        doc.weakness_task_id = None
        if doc.weakness_extracted_at is None:
            doc.weakness_error = doc_error[:500]
    if row.assessment_id is None:
        return
    a = db.get(Assessment, row.assessment_id)
    if a is None:
        return
    state = dict(a.phase_state or {})
    changed = False
    for phase, entry in state.items():
        if isinstance(entry, dict) and entry.get("task_id") == row.id:
            entry = dict(entry)
            entry["task_id"] = None
            entry["error"] = row.error[:500]
            state[phase] = entry
            changed = True
    if changed:
        a.phase_state = state


def reconcile_interrupted_tasks() -> int:
    """Startup: any task still pending/running in the table belonged to a
    previous process and can never finish. Mark it errored and fail the
    phase that owns it so the UI offers a re-run. Returns the count."""
    from app.db import SessionLocal
    from app.models import TaskRecord

    n = 0
    with SessionLocal() as db:
        rows = (
            db.query(TaskRecord)
            .filter(TaskRecord.status.in_(["pending", "running"]))
            .all()
        )
        for row in rows:
            _fail_owned_state(
                db, row, "interrupted by server restart — re-run the step",
                doc_error="interrupted by server restart — retry extraction",
            )
            n += 1
        db.commit()
    return n


# ---------- Phase-state helpers ----------
#
# Persistent per-phase markers on Assessment.phase_state. Each long-running
# orchestrator (scenarios_generation, cross_correlation, gap_analysis,
# narratives) calls mark_phase_started at submit time and mark_phase_done /
# mark_phase_error from inside the job. Survives server restarts so the UI
# can render "running / done / failed" without relying on volatile React state.
# The entry may also carry `stale` (see app.workflow.invalidate_downstream);
# starting a run clears it — the new run is, by definition, current.


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def mark_phase_started(assessment_id: int, phase: str, task_id: str) -> None:
    from app.db import SessionLocal
    from app.models import Assessment

    with SessionLocal() as db:
        a = db.get(Assessment, assessment_id)
        if a is None:
            return
        state = dict(a.phase_state or {})
        state[phase] = {
            "started_at": _now_iso(),
            "completed_at": None,
            "task_id": task_id,
            "error": None,
            "stale": None,
        }
        a.phase_state = state
        db.commit()


def mark_phase_done(
    assessment_id: int,
    phase: str,
    *,
    warning: str | None = None,
    failed_targets: list[str] | None = None,
) -> None:
    """Phase completed. `warning` / `failed_targets` record a partial success
    (e.g. gap analysis with N controls that could not be assessed) without
    turning the whole phase into an error."""
    from app.db import SessionLocal
    from app.models import Assessment

    with SessionLocal() as db:
        a = db.get(Assessment, assessment_id)
        if a is None:
            return
        state = dict(a.phase_state or {})
        existing = dict(state.get(phase, {}))
        existing["completed_at"] = _now_iso()
        existing["task_id"] = None
        existing["error"] = None
        existing["warning"] = (warning or "")[:500] or None
        existing["failed_targets"] = list(failed_targets or [])
        existing.setdefault("started_at", existing["completed_at"])
        state[phase] = existing
        a.phase_state = state
        db.commit()


def mark_phase_error(assessment_id: int, phase: str, err: str) -> None:
    from app.db import SessionLocal
    from app.models import Assessment

    with SessionLocal() as db:
        a = db.get(Assessment, assessment_id)
        if a is None:
            return
        state = dict(a.phase_state or {})
        existing = dict(state.get(phase, {}))
        existing["task_id"] = None
        existing["error"] = err[:500]
        state[phase] = existing
        a.phase_state = state
        db.commit()


def update_failed_targets(assessment_id: int, failed: list[str], warning: str | None) -> None:
    """Keep gap_analysis.failed_targets in step after a per-control re-run or
    a control deletion. No-op when the phase never completed."""
    from app.db import SessionLocal
    from app.models import Assessment

    with SessionLocal() as db:
        a = db.get(Assessment, assessment_id)
        if a is None:
            return
        state = dict(a.phase_state or {})
        entry = dict(state.get("gap_analysis") or {})
        if not entry.get("completed_at"):
            return
        entry["failed_targets"] = failed
        entry["warning"] = warning
        state["gap_analysis"] = entry
        a.phase_state = state
        db.commit()
