"""In-memory task registry for long-running pipeline runs.

Single-process, single-user deployment — so a dict + asyncio is enough.
Each task can emit events that SSE clients subscribe to.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
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
