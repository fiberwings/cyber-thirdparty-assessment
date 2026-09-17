"""Sequential workflow enforcement (app.workflow): prerequisites, mutual
exclusion and the 409 contract.

Each test seeds the durable state a real run leaves behind (conftest
`advance_workflow` / `seed_running_task`) and asserts that the guard fires
*before* any model call — `fake.calls == []` — so a refused request can never
have started a job.
"""

from __future__ import annotations

import io
import time

import pymupdf
import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Assessment, Document
from app.workflow import (
    KIND_CORRELATION,
    KIND_EXTRACTION,
    KIND_GAP,
    KIND_NARRATIVES,
    KIND_SCENARIOS,
)

from .conftest import FakeLLMClient, advance_workflow, seed_running_task

T1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture()
def fake(monkeypatch, fresh_db):
    f = FakeLLMClient()
    import app.ai.router as router_mod

    monkeypatch.setattr(router_mod, "LLMClient", lambda *a, **kw: f)
    return f


@pytest.fixture()
def client(fake):
    from app.main import app

    return TestClient(app)


def _new(client, *, description=True) -> int:
    r = client.post("/api/assessments", json={"vendor_name": "Acme"})
    aid = r.json()["id"]
    if description:
        r = client.post(
            f"/api/assessments/{aid}/description",
            json={"text": "SaaS billing vendor processing EU PII via REST APIs."},
        )
        assert r.status_code == 200
    return aid


def _pdf() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Admins must use MFA.")
    return pdf.tobytes()


def _upload(client, aid):
    return client.post(
        f"/api/assessments/{aid}/documents",
        data={"kind": "policy"},
        files={"file": ("policy.pdf", io.BytesIO(_pdf()), "application/pdf")},
    )


def _wait(client, tid, n=30):
    for _ in range(n):
        s = client.get(f"/api/tasks/{tid}").json()
        if s["status"] in {"done", "error"}:
            return s
        time.sleep(0.1)
    return s


def _conflict(r, code, step=None):
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == code, detail
    assert detail["missing"][0]["code"] == code
    if step is not None:
        assert detail["missing"][0]["step"] == step, detail
    assert detail["message"]
    return detail


# ---------- prerequisite table, one row per step ----------


def test_scoping_requires_description(client, fake):
    aid = _new(client, description=False)
    _conflict(client.post(f"/api/assessments/{aid}/scoping/turn", json={"answer": "x"}), "missing_description", "scoping")
    _conflict(client.post(f"/api/assessments/{aid}/scoping/force-continue"), "missing_description", "scoping")
    assert fake.calls == []


def test_scenarios_require_scoping_done(client, fake):
    aid = _new(client)
    d = _conflict(client.post(f"/api/assessments/{aid}/scenarios/generate"), "prerequisite_pending", "scoping")
    assert "scoping" in d["message"].lower()
    assert fake.calls == []
    # force-continue satisfies scoping
    assert client.post(f"/api/assessments/{aid}/scoping/force-continue").status_code == 200
    phases = client.get(f"/api/assessments/{aid}").json()["phases"]
    assert phases["scoping"]["state"] == "done" and phases["scenarios"]["ready"] is True


def test_evidence_upload_requires_scenarios(client, fake):
    aid = _new(client)
    _conflict(_upload(client, aid), "prerequisite_pending", "scoping")
    advance_workflow(aid, "scoping")
    _conflict(_upload(client, aid), "prerequisite_pending", "scenarios")
    assert fake.calls == []
    with SessionLocal() as db:
        assert db.query(Document).count() == 0


def test_correlation_requires_documents_and_complete_extraction(client, fake):
    aid = _new(client)
    advance_workflow(aid, "scenarios")
    _conflict(client.post(f"/api/assessments/{aid}/cross-correlate"), "no_documents", "evidence")

    # extraction still running → incomplete
    with SessionLocal() as db:
        db.add(Document(assessment_id=aid, kind="policy", filename="p.pdf", mime="application/pdf",
                        sha256="1" * 64, size_bytes=1, parsed_at=__import__("datetime").datetime.utcnow(),
                        weakness_task_id=T1))
        db.commit()
        doc_id = db.query(Document).one().id
    seed_running_task(aid, KIND_EXTRACTION, T1)
    d = _conflict(client.post(f"/api/assessments/{aid}/weaknesses/synthesize"), "extraction_incomplete", "evidence")
    assert d["missing"][0]["document_ids"] == [doc_id]
    assert client.get(f"/api/assessments/{aid}").json()["phases"]["evidence"]["state"] == "running"

    # extraction failed → 409 with the document listed, phase error + failed_targets
    with SessionLocal() as db:
        from app.models import TaskRecord
        db.get(TaskRecord, T1).status = "error"
        d_ = db.get(Document, doc_id)
        d_.weakness_error = "Extraction failed: boom"
        db.commit()
    d = _conflict(client.post(f"/api/assessments/{aid}/cross-correlate"), "extraction_failed", "evidence")
    assert d["missing"][0]["document_ids"] == [doc_id]
    assert "p.pdf" in d["missing"][0]["message"]
    a = client.get(f"/api/assessments/{aid}").json()
    assert a["phases"]["evidence"]["state"] == "error"
    assert a["phases"]["evidence"]["failed_targets"] == ["p.pdf"]
    docs = client.get(f"/api/assessments/{aid}/documents").json()
    assert docs[0]["extraction_state"] == "error" and "boom" in docs[0]["weakness_error"]
    assert fake.calls == []


def test_gap_analysis_requires_correlation(client, fake):
    aid = _new(client)
    advance_workflow(aid, "evidence")
    _conflict(client.post(f"/api/assessments/{aid}/gap-analysis/run"), "prerequisite_pending", "correlation")
    assert fake.calls == []
    # correlation errored → prerequisite_error
    from app.tasks import mark_phase_error, mark_phase_started
    mark_phase_started(aid, "cross_correlation", "dead")
    mark_phase_error(aid, "cross_correlation", "kaboom")
    _conflict(client.post(f"/api/assessments/{aid}/gap-analysis/run"), "prerequisite_error", "correlation")


def test_resume_requires_a_previous_run(client, fake):
    aid = _new(client)
    advance_workflow(aid, "correlation")
    _conflict(
        client.post(f"/api/assessments/{aid}/gap-analysis/run?only_failed=true"),
        "resume_requires_full_run",
        "analysis",
    )
    ec_id = client.get(f"/api/assessments/{aid}/scenarios").json()[0]["expected_controls"][0]["id"]
    _conflict(client.post(f"/api/expected-controls/{ec_id}/assess-ai"), "resume_requires_full_run", "analysis")
    assert fake.calls == []


def test_resume_allowed_after_errored_full_run(client, fake):
    """Regression: a full run that failed still counts as a run to resume."""
    aid = _new(client)
    advance_workflow(aid, "correlation")
    from app.tasks import mark_phase_error, mark_phase_started
    mark_phase_started(aid, "gap_analysis", "dead")
    mark_phase_error(aid, "gap_analysis", "all failed")
    assert client.get(f"/api/assessments/{aid}").json()["phases"]["analysis"]["state"] == "error"
    r = client.post(f"/api/assessments/{aid}/gap-analysis/run?only_failed=true")
    assert r.status_code == 200, r.text
    _wait(client, r.json()["task_id"])


def test_narratives_and_summary_require_analysis(client, fake):
    aid = _new(client)
    advance_workflow(aid, "correlation")
    _conflict(client.post(f"/api/assessments/{aid}/narratives/run"), "prerequisite_pending", "analysis")
    _conflict(client.post(f"/api/assessments/{aid}/executive-summary/run"), "prerequisite_pending", "analysis")
    assert fake.calls == []


def test_partial_gap_analysis_satisfies_narratives(client, fake):
    aid = _new(client)
    advance_workflow(aid, "correlation")
    from app.tasks import mark_phase_done
    mark_phase_done(aid, "gap_analysis", warning="1 control failed", failed_targets=["DATA_LEAK/ENC.REST"])
    phases = client.get(f"/api/assessments/{aid}").json()["phases"]
    assert phases["analysis"]["state"] == "done" and phases["analysis"]["warning"]
    assert phases["score"]["ready"] is True


def test_recalculate_and_model_overrides_are_unguarded(client, fake):
    aid = _new(client)
    assert client.post(f"/api/assessments/{aid}/recalculate").status_code == 200
    seed_running_task(aid, KIND_GAP, T1, phase="gap_analysis")
    assert client.post(f"/api/assessments/{aid}/recalculate").status_code == 200


# ---------- in-flight matrix ----------


def test_edit_during_gap_run_is_refused(client, fake):
    aid = _new(client)
    advance_workflow(aid, "correlation")
    seed_running_task(aid, KIND_GAP, T1, phase="gap_analysis")
    sc = client.get(f"/api/assessments/{aid}/scenarios").json()[0]
    ec_id = sc["expected_controls"][0]["id"]
    for r in (
        client.patch(f"/api/scenarios/{sc['id']}", json={"inherent_impact": 4}),
        client.delete(f"/api/scenarios/{sc['id']}"),
        client.patch(f"/api/expected-controls/{ec_id}", json={"weight": 0.5}),
        client.post(f"/api/scenarios/{sc['id']}/expected-controls", json={"code": "NEW", "name": "New"}),
        client.delete(f"/api/expected-controls/{ec_id}"),
        client.post(f"/api/expected-controls/{ec_id}/assess", json={"coverage": "none"}),
        client.post(f"/api/assessments/{aid}/description", json={"text": "Changed description text."}),
        client.patch(f"/api/assessments/{aid}/settings", json={"as_of_date": "2026-01-01"}),
        client.post(f"/api/assessments/{aid}/scoping/turn", json={"answer": "x"}),
        client.post(f"/api/assessments/{aid}/scenarios/generate"),
        client.post(f"/api/assessments/{aid}/cross-correlate"),
        _upload(client, aid),
    ):
        d = _conflict(r, "run_in_flight")
        assert d["missing"][0]["task_id"] == T1 and d["missing"][0]["kind"] == KIND_GAP
    # Downstream of the running step the readiness check answers first: the
    # prerequisite is still running rather than "another job is in flight".
    d = _conflict(client.post(f"/api/assessments/{aid}/executive-summary/run"), "prerequisite_running", "analysis")
    assert d["missing"][0]["task_id"] == T1
    docs = client.get(f"/api/assessments/{aid}/documents").json()
    assert [x["filename"] for x in docs] == ["policy.txt"]
    with SessionLocal() as db:
        a = db.get(Assessment, aid)
        assert len(a.scenarios) == 1 and len(a.scenarios[0].expected_controls) == 1
    assert fake.calls == []
    # readiness mirrors the guard
    phases = client.get(f"/api/assessments/{aid}").json()["phases"]
    assert phases["analysis"]["state"] == "running"
    assert phases["correlation"]["ready"] is False
    assert phases["correlation"]["blocked_by"][0]["code"] == "run_in_flight"


def test_same_step_reattaches(client, fake):
    aid = _new(client)
    advance_workflow(aid, "analysis")
    seed_running_task(aid, KIND_CORRELATION, T1, phase="cross_correlation")
    r = client.post(f"/api/assessments/{aid}/cross-correlate")
    assert r.status_code == 200 and r.json()["task_id"] == T1
    r = client.post(f"/api/assessments/{aid}/weaknesses/synthesize")
    assert r.status_code == 200 and r.json()["task_id"] == T1
    _conflict(client.post(f"/api/assessments/{aid}/gap-analysis/run"), "prerequisite_running", "correlation")

    with SessionLocal() as db:
        from app.models import TaskRecord
        db.get(TaskRecord, T1).status = "done"
        db.commit()
    from app.tasks import mark_phase_done
    mark_phase_done(aid, "cross_correlation")
    seed_running_task(aid, KIND_NARRATIVES, "b" * 8 + "-bbbb-bbbb-bbbb-bbbbbbbbbbbb", phase="narratives")
    r = client.post(f"/api/assessments/{aid}/narratives/run")
    assert r.status_code == 200 and r.json()["task_id"].startswith("bbbbbbbb")
    assert fake.calls == []


def test_upload_allowed_while_another_document_extracts(client, fake):
    aid = _new(client)
    advance_workflow(aid, "scenarios")
    seed_running_task(aid, KIND_EXTRACTION, T1)
    fake.push_json({"weaknesses": []})
    r = _upload(client, aid)
    assert r.status_code == 201, r.text
    assert r.json()["weakness_task_id"] and r.json()["weakness_task_id"] != T1
    _wait(client, r.json()["weakness_task_id"])
    # …but a scenario regeneration is not (a job is running)
    _conflict(client.post(f"/api/assessments/{aid}/scenarios/generate"), "run_in_flight")


def test_generate_during_scoping_turn_is_refused(client, fake):
    aid = _new(client)
    advance_workflow(aid, "scoping")
    seed_running_task(aid, "scoping_turn", T1)
    d = _conflict(client.post(f"/api/assessments/{aid}/scenarios/generate"), "run_in_flight")
    assert d["missing"][0]["kind"] == "scoping_turn"
    assert fake.calls == []


def test_retry_extraction_reattaches_to_live_task(client, fake):
    aid = _new(client)
    advance_workflow(aid, "scenarios")
    with SessionLocal() as db:
        db.add(Document(assessment_id=aid, kind="policy", filename="p.pdf", mime="application/pdf",
                        sha256="1" * 64, size_bytes=1, weakness_task_id=T1))
        db.commit()
        doc_id = db.query(Document).one().id
    seed_running_task(aid, KIND_EXTRACTION, T1)
    r = client.post(f"/api/documents/{doc_id}/extract-weaknesses")
    assert r.status_code == 200 and r.json()["task_id"] == T1
    assert fake.calls == []


def test_cross_correlate_writes_phase_markers(client, fake):
    """The `/cross-correlate` route used to skip the phase markers that the
    `synthesize` alias wrote; both now share one submit path."""
    aid = _new(client)
    advance_workflow(aid, "evidence")
    r = client.post(f"/api/assessments/{aid}/cross-correlate")
    assert r.status_code == 200, r.text
    assert client.get(f"/api/assessments/{aid}").json()["phases"]["correlation"]["state"] in {"running", "done"}
    s = _wait(client, r.json()["task_id"])
    assert s["status"] == "done", s
    phases = client.get(f"/api/assessments/{aid}").json()["phases"]
    assert phases["correlation"]["state"] == "done" and phases["correlation"]["completed_at"]
    assert phases["analysis"]["ready"] is True


def test_second_gap_analysis_click_reattaches(client, fake):
    """A double click must not start two runs over the same rows (the fake
    LLM finishes too fast to race two real submits, so the first click is
    represented by its durable state)."""
    aid = _new(client)
    advance_workflow(aid, "correlation")
    seed_running_task(aid, KIND_GAP, T1, phase="gap_analysis")
    r = client.post(f"/api/assessments/{aid}/gap-analysis/run")
    assert r.status_code == 200 and r.json()["task_id"] == T1
    r = client.post(f"/api/assessments/{aid}/gap-analysis/run?only_failed=true")
    assert r.status_code == 200 and r.json()["task_id"] == T1
    assert fake.calls == []


def test_current_phase_is_derived(client, fake):
    aid = _new(client)
    assert client.get(f"/api/assessments/{aid}").json()["current_phase"] == "scoping"
    advance_workflow(aid, "scenarios")
    assert client.get(f"/api/assessments/{aid}").json()["current_phase"] == "evidence"
    advance_workflow(aid, "evidence")
    assert client.get(f"/api/assessments/{aid}").json()["current_phase"] == "analysis"
    advance_workflow(aid, "narratives")
    assert client.get(f"/api/assessments/{aid}").json()["current_phase"] == "report"
