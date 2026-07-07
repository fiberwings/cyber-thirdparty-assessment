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
    out = client.wait_task("t1", "scenarios", timeout_s=10, poll_interval=0.01)
    assert out["status"] == "done"


@respx.mock
def test_wait_task_error_raises(client):
    respx.get(f"{BASE}/api/tasks/t1").mock(
        return_value=Response(200, json={"task_id": "t1", "status": "error", "progress": 0, "detail": "boom"})
    )
    with pytest.raises(StageError) as ei:
        client.wait_task("t1", "gap_analysis", timeout_s=10, poll_interval=0.01)
    assert ei.value.stage == "gap_analysis"
    assert "boom" in ei.value.detail


@respx.mock
def test_wait_task_timeout(client):
    respx.get(f"{BASE}/api/tasks/t1").mock(
        return_value=Response(200, json={"task_id": "t1", "status": "running", "progress": 0.1, "detail": ""})
    )
    with pytest.raises(StageError, match="timed out"):
        client.wait_task("t1", "narratives", timeout_s=0.05, poll_interval=0.01)


@respx.mock
def test_wait_task_404_falls_back_to_done_phase(client):
    respx.get(f"{BASE}/api/tasks/t1").mock(return_value=Response(404))
    respx.get(f"{BASE}/api/assessments/7").mock(
        return_value=Response(200, json={"id": 7, "phases": {"scenarios": {"state": "done"}}})
    )
    out = client.wait_task("t1", "scenarios", timeout_s=10, assessment_id=7, poll_interval=0.01)
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
        client.wait_task("t1", "gap_analysis", timeout_s=10, assessment_id=7, poll_interval=0.01)
