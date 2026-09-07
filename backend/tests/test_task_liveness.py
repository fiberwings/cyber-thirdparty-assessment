"""Task liveness: activity stamps, the watchdog, and cancellation.

A task proves it is alive through `TaskHandle.touch()` — called by progress
updates and, via the ambient `app.activity.current_task`, by every streamed
line the LLM router receives for the job. The watchdog cancels tasks that go
quiet or run past the ceiling; a cancelled task fails the state it owned
exactly like a restart would, so nothing stays wedged.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from app import activity
from app.ai.router import OpenRouterClient
from app.config import settings
from app.db import SessionLocal
from app.models import Assessment, Document, TaskRecord
from app.tasks import mark_phase_started, registry
from app.workflow import in_flight

BASE = "https://openrouter.ai/api/v1"
URL = f"{BASE}/chat/completions"


def _sse(*texts: str) -> bytes:
    out = []
    for t in texts:
        ev = {"choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]}
        out.append("data: " + json.dumps(ev) + "\n\n")
    out.append("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}}) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def _seed_assessment() -> tuple[int, int]:
    """Assessment + one document."""
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        d = Document(assessment_id=a.id, kind="policy", filename="p.docx", mime="x", sha256="s", size_bytes=1)
        db.add(d)
        db.commit()
        return a.id, d.id


def _own(aid: int, did: int, task_id: str) -> None:
    """The scenarios phase and the document's extraction are owned by task_id."""
    mark_phase_started(aid, "scenarios_generation", task_id)
    with SessionLocal() as db:
        db.get(Document, did).weakness_task_id = task_id
        db.commit()


async def _settle(task, timeout=1.0):
    """Let the wrapped job finish its cancellation bookkeeping."""
    try:
        await asyncio.wait_for(task._task, timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


# ---------------------------------------------------------------- activity stamps


async def test_touch_updates_memory_always_and_row_throttled(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "task_activity_persist_s", 0.2)
    gate = asyncio.Event()

    async def job(handle):
        await gate.wait()

    task = registry.submit(job, kind="t")
    await asyncio.sleep(0)
    first_mem = task.last_activity_at
    with SessionLocal() as db:
        first_row = db.get(TaskRecord, task.id).last_activity_at
    await asyncio.sleep(0.05)
    for _ in range(5):
        task.touch()
    assert task.last_activity_at > first_mem
    with SessionLocal() as db:
        assert db.get(TaskRecord, task.id).last_activity_at == first_row  # throttled
    await asyncio.sleep(0.25)
    task.touch()
    with SessionLocal() as db:
        assert db.get(TaskRecord, task.id).last_activity_at > first_row
    gate.set()
    await _settle(task)


async def test_submit_retains_task_and_sets_ambient_handle(fresh_db):
    seen = {}

    async def job(handle):
        seen["ambient"] = activity.current_task.get()

        async def child():
            seen["child"] = activity.current_task.get()

        await asyncio.gather(child())

    task = registry.submit(job, kind="t")
    assert isinstance(task._task, asyncio.Task)
    await _settle(task)
    assert seen["ambient"] is task and seen["child"] is task  # inherited by fan-out
    assert activity.current_task.get() is None  # reset after the job
    assert task.status == "done"


@respx.mock
async def test_streamed_call_touches_the_ambient_task(fresh_db, monkeypatch):
    respx.post(URL).mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_sse("a", "b", "c")))
    touches = []
    gate = asyncio.Event()

    async def job(handle):
        monkeypatch.setattr(handle, "touch", lambda: touches.append(1))
        await OpenRouterClient(api_key="k", base_url=BASE).chat([{"role": "user", "content": "x"}], "m")
        await gate.wait()

    task = registry.submit(job, kind="t")
    await asyncio.sleep(0.1)
    assert len(touches) >= 3
    gate.set()
    await _settle(task)


async def test_status_payloads_carry_liveness(fresh_db):
    from app.main import app

    gate = asyncio.Event()

    async def job(handle):
        await gate.wait()

    aid, did = _seed_assessment()
    task = registry.submit(job, kind="scenarios", assessment_id=aid)
    _own(aid, did, task.id)
    await asyncio.sleep(0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        st = (await c.get(f"/api/tasks/{task.id}")).json()
        assert st["status"] == "running" and st["last_activity_at"] and isinstance(st["idle_s"], float)
        assert st["kind"] == "scenarios"
        phase = (await c.get(f"/api/assessments/{aid}")).json()["phases"]["scenarios"]
        assert phase["state"] == "running" and isinstance(phase["idle_s"], float)
        gate.set()
        await _settle(task)
        st = (await c.get(f"/api/tasks/{task.id}")).json()
        assert st["status"] == "done" and st["idle_s"] is None


# ---------------------------------------------------------------- watchdog


async def test_watchdog_cancels_idle_task_and_fails_owned_state(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "task_idle_timeout_s", 0.05)
    gate = asyncio.Event()

    async def job(handle):
        await gate.wait()

    aid, did = _seed_assessment()
    task = registry.submit(job, kind="scenarios", assessment_id=aid)
    _own(aid, did, task.id)
    await asyncio.sleep(0.1)
    cancelled = await registry.watchdog_tick()
    assert cancelled == [task.id]
    await _settle(task)
    assert task.status == "error" and "no activity" in task.error and "TASK_IDLE_TIMEOUT_S" in task.error
    with SessionLocal() as db:
        row = db.get(TaskRecord, task.id)
        assert row.status == "error" and "no activity" in row.error
        a = db.get(Assessment, aid)
        entry = a.phase_state["scenarios_generation"]
        assert entry["task_id"] is None and "no activity" in entry["error"]
        d = db.get(Document, did)
        assert d.weakness_task_id is None and "retry extraction" in d.weakness_error
        assert in_flight(a) == []
    # A second tick is a no-op.
    assert await registry.watchdog_tick() == []


async def test_watchdog_cancels_runaway_task_even_when_active(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "task_max_runtime_s", 0.05)
    gate = asyncio.Event()

    async def job(handle):
        while not gate.is_set():
            handle.touch()
            await asyncio.sleep(0.01)

    task = registry.submit(job, kind="t")
    await asyncio.sleep(0.1)
    assert await registry.watchdog_tick() == [task.id]
    await _settle(task)
    assert task.status == "error" and "TASK_MAX_RUNTIME_S" in task.error


async def test_watchdog_leaves_active_task_alone(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "task_idle_timeout_s", 0.2)
    gate = asyncio.Event()

    async def job(handle):
        while not gate.is_set():
            handle.touch()
            await asyncio.sleep(0.01)

    task = registry.submit(job, kind="t")
    await asyncio.sleep(0.3)
    assert await registry.watchdog_tick() == []
    assert task.status == "running"
    gate.set()
    await _settle(task)
    assert task.status == "done"


# ---------------------------------------------------------------- cancellation


async def test_cancel_endpoint_frees_the_assessment(fresh_db):
    from app.main import app

    gate = asyncio.Event()

    async def job(handle):
        await gate.wait()

    aid, did = _seed_assessment()
    task = registry.submit(job, kind="scenarios", assessment_id=aid)
    _own(aid, did, task.id)
    await asyncio.sleep(0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/api/tasks/{task.id}/cancel")
        assert r.status_code == 202, r.text
        await _settle(task)
        st = (await c.get(f"/api/tasks/{task.id}")).json()
        assert st["status"] == "error" and "cancelled by user" in st["detail"]
        # Already finished → 409; unknown → 404.
        assert (await c.post(f"/api/tasks/{task.id}/cancel")).status_code == 409
        assert (await c.post("/api/tasks/nope/cancel")).status_code == 404
        phase = (await c.get(f"/api/assessments/{aid}")).json()["phases"]["scenarios"]
        assert phase["state"] == "error" and "cancelled by user" in phase["error"]
    with SessionLocal() as db:
        assert in_flight(db.get(Assessment, aid)) == []


async def test_cancel_of_detached_snapshot_is_refused(fresh_db):
    from app.main import app

    with SessionLocal() as db:
        db.add(TaskRecord(id="other-proc", status="running", kind="t"))
        db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/tasks/other-proc/cancel")
        assert r.status_code == 409 and "not cancellable" in r.json()["detail"]


class _StallingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield b'data: {"choices":[{"index":0,"delta":{"content":"par"},"finish_reason":null}]}\n\n'
        await asyncio.sleep(10)

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
async def test_cancel_closes_in_flight_stream_and_records_call(fresh_db):
    from app.ai.router import call_structured
    from app.models import ModelCall
    from pydantic import BaseModel

    class _Out(BaseModel):
        answer: str

    stream = _StallingStream()
    respx.post(URL).mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=stream))

    async def job(handle):
        with SessionLocal() as db:
            await call_structured(db, purpose="t", profile="fast", messages=[{"role": "user", "content": "x"}],
                                  schema=_Out, client=OpenRouterClient(api_key="k", base_url=BASE))

    task = registry.submit(job, kind="t")
    await asyncio.sleep(0.1)
    assert registry.cancel(task.id, reason="test")
    await _settle(task)
    assert stream.closed is True
    assert task.status == "error" and task.error == "cancelled: test"
    with SessionLocal() as db:
        row = db.query(ModelCall).one()
        assert row.ok is False and row.error == "cancelled"
