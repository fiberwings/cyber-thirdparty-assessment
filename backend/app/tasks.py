"""Task registry for long-running pipeline runs.

Single-process, single-user deployment — a dict + asyncio drives the live
side (progress events, SSE). Every status change is also written through to
the `task` table so `GET /api/tasks/{id}` keeps answering after a server
restart: tasks that were pending/running when the process died are marked
`error` ("interrupted by server restart") at startup, and the phase that
owned them is marked failed so the UI offers a re-run instead of hanging.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Awaitable, Callable

JobFn = Callable[["TaskHandle"], Awaitable[Any]]


@dataclass
class TaskEvent:
    ts: float
    event: str
    data: dict


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
    _cv: asyncio.Condition = field(default_factory=asyncio.Condition)

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
                db.commit()
        except Exception:
            pass

    def set(self, *, status: str | None = None, progress: float | None = None,
            detail: str | None = None, event: str = "progress",
            data: dict | None = None) -> None:
        """Synchronous progress update — safe to call from anywhere."""
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
                return TaskHandle(
                    id=row.id,
                    status=row.status,
                    progress=row.progress,
                    detail=row.detail,
                    error=row.error,
                    kind=row.kind,
                    assessment_id=row.assessment_id,
                )
        except Exception:
            return None

    def active(self, assessment_id: int, *, exclude_kinds: tuple[str, ...] = ()) -> list[TaskHandle]:
        """Live pending/running tasks of this process for one assessment.

        The in-memory dict is the truth for the single-process deployment;
        tasks from a previous process are reconciled to `error` at startup, so
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
            await task.update(status="running", event="start")
            try:
                task.result = await fn(task)
                await task.update(status="done", progress=1.0, event="done")
            except Exception as e:
                task.error = str(e)
                await task.update(status="error", detail=str(e)[:300], event="error")

        asyncio.create_task(_wrap())
        return task

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
                    yield TaskEvent(time.time(), "heartbeat", {"status": task.status})


registry = TaskRegistry()


def reconcile_interrupted_tasks() -> int:
    """Startup: any task still pending/running in the table belonged to a
    previous process and can never finish. Mark it errored and fail the
    phase that owns it so the UI offers a re-run. Returns the count."""
    from app.db import SessionLocal
    from app.models import Assessment, Document, TaskRecord

    n = 0
    with SessionLocal() as db:
        rows = (
            db.query(TaskRecord)
            .filter(TaskRecord.status.in_(["pending", "running"]))
            .all()
        )
        for row in rows:
            row.status = "error"
            row.error = "interrupted by server restart — re-run the step"
            row.detail = row.error
            n += 1
            # Per-document extraction: surface the interruption on the document
            # so the evidence phase shows a retryable error, not a silent gap.
            for doc in (
                db.query(Document)
                .filter(Document.weakness_task_id == row.id)
                .all()
            ):
                doc.weakness_task_id = None
                if doc.weakness_extracted_at is None:
                    doc.weakness_error = "interrupted by server restart — retry extraction"
            if row.assessment_id is None:
                continue
            a = db.get(Assessment, row.assessment_id)
            if a is None:
                continue
            state = dict(a.phase_state or {})
            changed = False
            for phase, entry in state.items():
                if isinstance(entry, dict) and entry.get("task_id") == row.id:
                    entry = dict(entry)
                    entry["task_id"] = None
                    entry["error"] = row.error
                    state[phase] = entry
                    changed = True
            if changed:
                a.phase_state = state
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
