from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from app.schemas.api import TaskStatusRead
from app.tasks import registry

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("/{task_id}", response_model=TaskStatusRead)
def task_status(task_id: str):
    t = registry.get(task_id)
    if t is None:
        raise HTTPException(status_code=404)
    return TaskStatusRead(
        task_id=t.id, status=t.status, progress=t.progress, detail=t.detail or t.error
    )
    # (handles for tasks of a previous process come back as detached snapshots)


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
