"""In-memory task registry for long-running pipeline runs.

Single-process, single-user deployment — so a dict + asyncio is enough.
Each task can emit events that SSE clients subscribe to.
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
    events: list[TaskEvent] = field(default_factory=list)
    _cv: asyncio.Condition = field(default_factory=asyncio.Condition)

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
        return self._tasks.get(task_id)

    def submit(self, fn: JobFn) -> TaskHandle:
        task = TaskHandle(id=str(uuid.uuid4()))
        self._tasks[task.id] = task

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


# ---------- Phase-state helpers ----------
#
# Persistent per-phase markers on Assessment.phase_state. Each long-running
# orchestrator (scenarios_generation, cross_correlation, gap_analysis,
# narratives) calls mark_phase_started at submit time and mark_phase_done /
# mark_phase_error from inside the job. Survives server restarts so the UI
# can render "running / done / failed" without relying on volatile React state.


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
        }
        a.phase_state = state
        db.commit()


def mark_phase_done(assessment_id: int, phase: str) -> None:
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
