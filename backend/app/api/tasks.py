from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.schemas.api import TaskStatusRead
from app.tasks import registry, task_status_read

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskStatusRead)
def task_status(task_id: str):
    t = registry.get(task_id)
    if t is None:
        raise HTTPException(status_code=404)
    # (handles for tasks of a previous process come back as detached snapshots)
    return task_status_read(t)


@router.post("/{task_id}/cancel", response_model=TaskStatusRead, status_code=202)
async def cancel_task(task_id: str):
    """Cancel a running job. The job's in-flight model stream is closed
    (which stops generation and billing on providers that support it — not
    all do; the app is freed either way), the task ends in `error`
    ("cancelled by user") and the step it owned becomes re-runnable. Must be
    async: asyncio task cancellation is not thread-safe."""
    t = registry.get(task_id)
    if t is None:
        raise HTTPException(status_code=404)
    if not t.live:
        raise HTTPException(status_code=409, detail="task already finished")
    if t.detached:
        raise HTTPException(status_code=409, detail="task not cancellable from this process")
    registry.cancel(task_id, reason="cancelled by user")
    return task_status_read(t)


@router.get("/{task_id}/events")
async def task_events(task_id: str):
    t = registry.get(task_id)
    if t is None:
        raise HTTPException(status_code=404)

    async def gen():
        async for ev in registry.stream(task_id):
            yield {
                "event": ev.event,
                "data": json.dumps(ev.data),
            }

    return EventSourceResponse(gen())
