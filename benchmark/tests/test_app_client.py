import pytest
import respx
from httpx import Response

from bench.app_client import AppClient, StageError

BASE = "http://testbackend:8000"


@pytest.fixture
def client():
    c = AppClient(BASE)
    yield c
    c.close()


@respx.mock
def test_wait_task_done(client):
    route = respx.get(f"{BASE}/api/tasks/t1")
    route.side_effect = [
        Response(200, json={"task_id": "t1", "status": "running", "progress": 0.5, "detail": ""}),
        Response(200, json={"task_id": "t1", "status": "done", "progress": 1.0, "detail": ""}),
    ]
    out = client.wait_task("t1", "scenarios", idle_timeout_s=10, poll_interval=0.01)
    assert out["status"] == "done"


@respx.mock
def test_wait_task_error_raises(client):
    respx.get(f"{BASE}/api/tasks/t1").mock(
        return_value=Response(200, json={"task_id": "t1", "status": "error", "progress": 0, "detail": "boom"})
    )
    with pytest.raises(StageError) as ei:
        client.wait_task("t1", "gap_analysis", idle_timeout_s=10, poll_interval=0.01)
    assert ei.value.stage == "gap_analysis"
    assert "boom" in ei.value.detail


@respx.mock
def test_wait_task_timeout(client):
    respx.get(f"{BASE}/api/tasks/t1").mock(
        return_value=Response(200, json={"task_id": "t1", "status": "running", "progress": 0.1, "detail": ""})
    )
    with pytest.raises(StageError, match="timed out"):
        client.wait_task("t1", "narratives", idle_timeout_s=0.05, poll_interval=0.01)


@respx.mock
def test_wait_task_404_falls_back_to_done_phase(client):
    respx.get(f"{BASE}/api/tasks/t1").mock(return_value=Response(404))
    respx.get(f"{BASE}/api/assessments/7").mock(
        return_value=Response(200, json={"id": 7, "phases": {"scenarios": {"state": "done"}}})
    )
    out = client.wait_task("t1", "scenarios", assessment_id=7, idle_timeout_s=10, poll_interval=0.01)
    assert out["status"] == "done"


@respx.mock
def test_wait_task_404_with_error_phase_raises(client):
    respx.get(f"{BASE}/api/tasks/t1").mock(return_value=Response(404))
    respx.get(f"{BASE}/api/assessments/7").mock(
        return_value=Response(
            200,
            json={"id": 7, "phases": {"analysis": {"state": "error", "error": "lost on restart"}}},
        )
    )
    with pytest.raises(StageError, match="task lost"):
        client.wait_task("t1", "gap_analysis", assessment_id=7, idle_timeout_s=10, poll_interval=0.01)


def _running(**extra):
    return Response(200, json={"task_id": "t1", "status": "running", "progress": 0.1, "detail": "working", **extra})


@respx.mock
def test_wait_task_idle_clock_resets_on_activity(client):
    """Six polls whose last_activity_at keeps advancing, each further apart
    than the idle limit, then done: liveness beats the clock."""
    route = respx.get(f"{BASE}/api/tasks/t1")
    route.side_effect = [_running(last_activity_at=f"2026-09-04T01:00:0{i}Z") for i in range(6)] + [
        Response(200, json={"task_id": "t1", "status": "done", "progress": 1.0, "detail": ""})
    ]
    out = client.wait_task("t1", "gap_analysis", idle_timeout_s=0.03, poll_interval=0.02)
    assert out["status"] == "done" and route.call_count == 7


@respx.mock
def test_wait_task_idle_timeout_names_the_setting(client):
    respx.get(f"{BASE}/api/tasks/t1").mock(return_value=_running(last_activity_at="2026-09-04T01:00:00Z"))
    with pytest.raises(StageError, match="no activity for .*IDLE_TIMEOUT_S"):
        client.wait_task("t1", "narratives", idle_timeout_s=0.05, poll_interval=0.01)


@respx.mock
def test_wait_task_ceiling_stops_a_runaway_stage(client):
    tick = {"n": 0}

    def alive(request):
        tick["n"] += 1
        return _running(last_activity_at=f"2026-09-04T01:00:{tick['n']:02d}Z")

    respx.get(f"{BASE}/api/tasks/t1").mock(side_effect=alive)
    with pytest.raises(StageError, match="MAX_STAGE_S"):
        client.wait_task("t1", "scenarios", idle_timeout_s=10, max_stage_s=0.05, poll_interval=0.01)


@respx.mock
def test_wait_task_old_backend_detail_change_counts_as_activity(client):
    """No last_activity_at field (older app): a changing detail still resets
    the idle clock."""
    route = respx.get(f"{BASE}/api/tasks/t1")
    route.side_effect = [_running(detail=f"step {i}") for i in range(4)] + [
        Response(200, json={"task_id": "t1", "status": "done", "progress": 1.0, "detail": ""})
    ]
    out = client.wait_task("t1", "scenarios", idle_timeout_s=0.03, poll_interval=0.02)
    assert out["status"] == "done" and route.call_count == 5
