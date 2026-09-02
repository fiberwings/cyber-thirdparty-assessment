"""Concurrency guard on scenarios/generate.

Two overlapping generation runs interleave destructively (each clears and
reinserts the description-sourced scenarios; phase-2 control writes then
attach to the other run's rows). The endpoint must re-attach a second request
to the run already in flight, and the phase serializer must keep reporting
"running" even though skeleton rows already exist mid-run.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from .conftest import FakeOpenRouterClient, seed_running_task

RUNNING_TASK_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def guarded_client(monkeypatch, fresh_db):
    fake = FakeOpenRouterClient()
    import app.ai.router as router_mod
    monkeypatch.setattr(router_mod, "OpenRouterClient", lambda *a, **kw: fake)
    return fake


def _mk_assessment(client) -> int:
    r = client.post("/api/assessments", json={"vendor_name": "Acme"})
    assert r.status_code == 201
    aid = r.json()["id"]
    r = client.post(
        f"/api/assessments/{aid}/description",
        json={"text": "SaaS billing vendor processing EU PII via REST APIs."},
    )
    assert r.status_code == 200
    # Scenario generation requires scoping to be done (workflow guard).
    r = client.post(f"/api/assessments/{aid}/scoping/force-continue")
    assert r.status_code == 200
    return aid


def _simulate_run_in_flight(aid: int) -> None:
    """A running task record plus the persistent phase marker pointing at it —
    the durable state a click leaves behind while its job is executing."""
    seed_running_task(aid, "scenarios_generation", RUNNING_TASK_ID, phase="scenarios_generation")


def test_second_generate_reattaches_to_running_task(guarded_client):
    from app.main import app

    client = TestClient(app)
    aid = _mk_assessment(client)
    _simulate_run_in_flight(aid)

    r = client.post(f"/api/assessments/{aid}/scenarios/generate")
    assert r.status_code == 200
    body = r.json()
    assert body["task_id"] == RUNNING_TASK_ID
    assert body["status"] == "running"
    # No second run was started: the fake LLM saw no calls.
    assert guarded_client.calls == []


def test_phase_reports_running_despite_midrun_scenario_rows(guarded_client):
    from app.db import SessionLocal
    from app.main import app
    from app.models import Scenario

    client = TestClient(app)
    aid = _mk_assessment(client)
    _simulate_run_in_flight(aid)

    # Mid-run persistence: skeleton rows exist before the run completes.
    with SessionLocal() as db:
        db.add(
            Scenario(
                assessment_id=aid,
                code="DATA_LEAK",
                name="Data leakage",
                description="skeleton",
                source="description",
                inherent_impact=3,
                inherent_likelihood=3,
                residual_impact=3,
                residual_likelihood=3,
                score_band="Moderate",
            )
        )
        db.commit()

    phase = client.get(f"/api/assessments/{aid}").json()["phases"]["scenarios"]
    assert phase["state"] == "running"
    assert phase["task_id"] == RUNNING_TASK_ID
    assert phase["progress"] == pytest.approx(0.2)


def test_generate_starts_fresh_after_prior_run_finished(guarded_client):
    from app.main import app
    from app.tasks import mark_phase_done

    client = TestClient(app)
    aid = _mk_assessment(client)
    _simulate_run_in_flight(aid)

    # Prior run completes: registry record done, phase marker cleared.
    from app.db import SessionLocal
    from app.models import TaskRecord

    with SessionLocal() as db:
        row = db.get(TaskRecord, RUNNING_TASK_ID)
        row.status = "done"
        row.progress = 1.0
        db.commit()
    mark_phase_done(aid, "scenarios_generation")

    guarded_client.push_json(
        {
            "scenarios": [
                {
                    "code": "DATA_LEAK",
                    "name": "Data leakage of customer PII",
                    "description": "Vendor mishandles billing PII.",
                    "inherent_impact": 3,
                    "inherent_likelihood": 3,
                }
            ]
        }
    )
    guarded_client.push_json(
        {
            "expected_controls": [
                {"code": "ENC.REST", "name": "Encryption at rest", "description": "", "weight": 1.0, "rationale": ""},
            ]
        }
    )

    r = client.post(f"/api/assessments/{aid}/scenarios/generate")
    assert r.status_code == 200
    task_id = r.json()["task_id"]
    assert task_id != RUNNING_TASK_ID

    for _ in range(30):
        s = client.get(f"/api/tasks/{task_id}").json()
        if s["status"] in {"done", "error"}:
            break
        time.sleep(0.2)
    assert s["status"] == "done", s

    scenarios = client.get(f"/api/assessments/{aid}/scenarios").json()
    assert [sc["code"] for sc in scenarios] == ["DATA_LEAK"]
    # Inherent band comes from the 4x4 matrix at insertion (3/3 -> High),
    # not the old hardcoded "Moderate" placeholder.
    assert scenarios[0]["score_band"] == "High"
