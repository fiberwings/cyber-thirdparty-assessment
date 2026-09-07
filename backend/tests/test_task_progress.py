"""Structured task progress: stages/units on the handle, the router's
per-call telemetry through `app.activity`, and the API payloads that carry
them. Observation only — none of this may influence what the job does.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from app import activity
from app.ai.router import OpenRouterClient
from app.db import SessionLocal
from app.models import TaskRecord
from app.tasks import TaskHandle, TaskStats, registry, task_status_read

BASE = "https://openrouter.ai/api/v1"
URL = f"{BASE}/chat/completions"


def _sse(*texts: str, reasoning: str = "", completion_tokens: int | None = 7) -> bytes:
    out = []
    if reasoning:
        ev = {"choices": [{"index": 0, "delta": {"reasoning": reasoning}, "finish_reason": None}]}
        out.append("data: " + json.dumps(ev) + "\n\n")
    for t in texts:
        ev = {"choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}]}
        out.append("data: " + json.dumps(ev) + "\n\n")
    final = {"choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}]}
    if completion_tokens is not None:
        final["usage"] = {
            "prompt_tokens": 1, "completion_tokens": completion_tokens, "cost": 0.0,
            "completion_tokens_details": {"reasoning_tokens": 3},
        }
    out.append("data: " + json.dumps(final) + "\n\n")
    out.append("data: [DONE]\n\n")
    return "".join(out).encode()


async def _settle(task, timeout=1.0):
    try:
        await asyncio.wait_for(task._task, timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


# ---------------------------------------------------------------- stages / units


def test_plan_stage_advance_derive_monotonic_progress(fresh_db):
    h = TaskHandle(id="t1", kind="x")
    seen: list[float] = []
    h.plan(["A", "B", "C"])
    assert h.stats.stages == ["A", "B", "C"] and h.stats.stage_index == -1
    h.stage("A", units_total=4, unit_label="docs")
    seen.append(h.progress)
    for i in range(1, 5):
        h.advance(i)
        seen.append(h.progress)
    assert h.stats.units_done == 4 and h.detail == "A"
    h.stage("B")  # indeterminate stage
    seen.append(h.progress)
    assert h.stats.units_total == 0 and h.stats.unit_label == ""
    h.stage("C", units_total=2, unit_label="clusters")
    h.advance()  # implicit +1
    seen.append(h.progress)
    assert h.stats.units_done == 1
    assert seen == sorted(seen), seen
    assert 0.0 <= seen[-1] < 1.0
    # A stage that was not planned is appended, never rejected.
    h.stage("D")
    assert h.stats.stages[-1] == "D" and h.stats.stage_index == 3


def test_unplanned_stages_never_move_progress_backwards(fresh_db):
    h = TaskHandle(id="t2", kind="x")
    h.stage("Only", units_total=10, unit_label="windows")
    h.advance(9)
    p = h.progress
    h.stage("Later")  # lazily appended: (1 + 0) / 2 = 0.5 < p
    assert h.progress >= p


def test_stats_round_trip_through_row_and_detached_snapshot(fresh_db):
    h = TaskHandle(id="t3", kind="x")
    h._persist()
    h.plan(["A"])
    h.stage("A", units_total=3, unit_label="controls")
    h.note_call_started("gap_analysis_control")
    h.note_delta(40)
    h.note_attempt_finished(12, 5)
    h.note_call_finished()
    h.advance(2)
    with SessionLocal() as db:
        row = db.get(TaskRecord, h.id)
        assert row.stats["units_done"] == 2 and row.stats["tokens_out"] == 12
        assert row.stats["purpose"] == "gap_analysis_control"
    registry._tasks.pop(h.id, None)
    snap = registry.get(h.id)
    assert snap is not None and snap.detached
    assert snap.stats.stage == "A" and snap.stats.units_total == 3
    assert snap.stats_dict()["tokens_out"] == 12 and snap.stats_dict()["tokens_reasoning"] == 5
    assert task_status_read(snap).calls_done == 1


def test_token_estimate_then_exact_reconcile(fresh_db):
    h = TaskHandle(id="t4", kind="x")
    h.note_delta(400)
    h.note_delta(100, reasoning=True)
    d = h.stats_dict()
    assert d["tokens_out"] == 100 and d["tokens_reasoning"] == 25  # chars // 4 while streaming
    h.note_attempt_finished(None, None)  # no usage chunk: the estimate is committed
    assert h.stats_dict()["tokens_out"] == 100
    h.note_delta(800)
    h.note_attempt_finished(150, 0)  # exact usage replaces the second attempt's estimate
    assert h.stats_dict()["tokens_out"] == 250


# ---------------------------------------------------------------- activity hooks


def test_activity_hooks_are_noops_without_a_task_and_never_raise(fresh_db):
    assert activity.current_task.get() is None
    activity.plan(["x"]); activity.stage("x", units_total=1); activity.advance()
    activity.call_started("p"); activity.note_delta(3); activity.note_first_token(1)
    activity.attempt_finished({"completion_tokens": 1}); activity.call_finished()

    class Broken(TaskHandle):
        def stage(self, *a, **k):  # noqa: D401
            raise RuntimeError("boom")

    token = activity.current_task.set(Broken(id="b", kind="x"))
    try:
        activity.stage("x")  # swallowed
    finally:
        activity.current_task.reset(token)


@respx.mock
async def test_streamed_call_feeds_task_telemetry(fresh_db):
    respx.post(URL).mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_sse("hello ", "world", reasoning="thinking...", completion_tokens=7)))
    gate = asyncio.Event()
    mid: dict = {}

    async def job(handle):
        from app.ai.router import call_text

        with SessionLocal() as db:
            out = await call_text(
                db, purpose="narrative", profile="reasoner",
                messages=[{"role": "user", "content": "x"}],
                client=OpenRouterClient(api_key="k", base_url=BASE),
            )
        mid["out"] = out
        mid["stats"] = handle.stats_dict()
        await gate.wait()

    task = registry.submit(job, kind="narratives")
    await asyncio.sleep(0.2)
    gate.set()
    await _settle(task)
    assert mid["out"] == "hello world"
    st = mid["stats"]
    assert st["purpose"] == "narrative"
    assert st["calls_done"] == 1 and st["calls_active"] == 0
    assert st["tokens_out"] == 7  # exact from the usage chunk, not chars // 4
    assert st["tokens_reasoning"] == 3
    assert isinstance(st["first_token_ms"], int)


@respx.mock
async def test_failed_attempt_keeps_the_estimate(fresh_db):
    # Mid-stream provider error after output started: the attempt is not
    # retried (partial) and the tokens it streamed stay counted.
    body = (
        "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "abcdefgh"}, "finish_reason": None}]}) + "\n\n"
        + "data: " + json.dumps({"error": {"code": 500, "message": "upstream"}}) + "\n\n"
    )
    respx.post(URL).mock(return_value=httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=body.encode()))
    seen: dict = {}

    async def job(handle):
        with pytest.raises(Exception):
            await OpenRouterClient(api_key="k", base_url=BASE).chat([{"role": "user", "content": "x"}], "m")
        seen["stats"] = handle.stats_dict()

    task = registry.submit(job, kind="t")
    await _settle(task)
    assert seen["stats"]["tokens_out"] == 2  # 8 chars // 4


# ---------------------------------------------------------------- API payloads


async def test_status_and_phase_payloads_carry_progress(fresh_db):
    from app.main import app
    from app.models import Assessment
    from app.tasks import mark_phase_started

    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.commit()
        aid = a.id

    gate = asyncio.Event()

    async def job(handle):
        handle.plan(["Assessing controls"])
        handle.stage("Assessing controls", units_total=24, unit_label="controls")
        handle.advance(7, detail="S1/C3")
        await gate.wait()

    task = registry.submit(job, kind="gap_analysis", assessment_id=aid)
    mark_phase_started(aid, "gap_analysis", task.id)
    await asyncio.sleep(0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        st = (await c.get(f"/api/tasks/{task.id}")).json()
        assert st["stage"] == "Assessing controls" and st["units_done"] == 7 and st["units_total"] == 24
        assert st["unit_label"] == "controls" and st["stages"] == ["Assessing controls"]
        assert isinstance(st["elapsed_s"], float) and st["detail"] == "S1/C3"
        phase = (await c.get(f"/api/assessments/{aid}")).json()["phases"]["analysis"]
        assert phase["state"] == "running" and phase["units_done"] == 7 and phase["stage"] == "Assessing controls"
        gate.set()
        await _settle(task)
        st = (await c.get(f"/api/tasks/{task.id}")).json()
        assert st["status"] == "done" and st["progress"] == 1.0
        assert st["units_done"] == 7  # the snapshot survives completion


def test_rows_without_stats_still_serialise(fresh_db):
    with SessionLocal() as db:
        db.add(TaskRecord(id="legacy", status="done", kind="x", stats={}))
        db.commit()
    snap = registry.get("legacy")
    assert snap is not None and snap.stats == TaskStats()
    assert task_status_read(snap).units_total == 0
