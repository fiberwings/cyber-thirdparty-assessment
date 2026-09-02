"""Stale-on-write invalidation (app.workflow.invalidate_downstream).

Every mutator that changes an input stamps the completed steps downstream of
it as `stale`: the result stays visible with the reason, and the prerequisite
guard blocks progression until the stamped step is re-run. These tests start
from a fully completed assessment (durable state stamped by
`advance_workflow`) and apply one mutator per test.
"""

from __future__ import annotations

import io
import time
from datetime import datetime

import pymupdf
import pytest
from fastapi.testclient import TestClient

from .conftest import FakeOpenRouterClient, advance_workflow, seed_running_task

from app.db import SessionLocal
from app.models import Assessment, Document

UI = ("scoping", "scenarios", "evidence", "correlation", "analysis", "score")
# Data-derived steps carry no phase entry and therefore no stamp of their own;
# they are blocked through the stamped step upstream of them.
UNSTAMPED = ("scoping", "evidence")


@pytest.fixture()
def fake(monkeypatch, fresh_db):
    f = FakeOpenRouterClient()
    import app.ai.router as router_mod

    monkeypatch.setattr(router_mod, "OpenRouterClient", lambda *a, **kw: f)
    return f


@pytest.fixture()
def client(fake):
    from app.main import app

    return TestClient(app)


def _complete(client) -> int:
    r = client.post("/api/assessments", json={"vendor_name": "Acme"})
    aid = r.json()["id"]
    r = client.post(
        f"/api/assessments/{aid}/description",
        json={"text": "SaaS billing vendor processing EU PII via REST APIs."},
    )
    assert r.status_code == 200
    advance_workflow(aid, "narratives")
    phases = _phases(client, aid)
    assert {k: v["state"] for k, v in phases.items()} == {k: "done" for k in UI}
    assert all(v["stale"] is None for v in phases.values())
    return aid


def _phases(client, aid) -> dict:
    return client.get(f"/api/assessments/{aid}").json()["phases"]


def _stale_map(phases) -> dict[str, list[str] | None]:
    """ui_key -> reasons (None when current)."""
    return {k: (v["stale"] or {}).get("reasons") if v["stale"] else None for k, v in phases.items()}


def _assert_stale_from(phases, first_stale: str, reason: str):
    """Steps before `first_stale` current, `first_stale` and everything after
    stale with `reason`; readiness follows (only the first stale step is ready
    to be re-run; downstream is blocked by it)."""
    idx = UI.index(first_stale)
    for k in UI[:idx]:
        assert phases[k]["stale"] is None, (k, phases[k])
    for k in UI[idx:]:
        assert phases[k]["state"] == "done", (k, phases[k])
        if k in UNSTAMPED:
            assert phases[k]["stale"] is None, (k, phases[k])
            continue
        assert phases[k]["stale"] is not None, (k, phases[k])
        assert reason in phases[k]["stale"]["reasons"], (k, phases[k]["stale"])
        assert phases[k]["stale"]["at"]
    assert phases[first_stale]["ready"] is True, phases[first_stale]
    for k in UI[idx + 1 :]:
        assert phases[k]["ready"] is False, (k, phases[k])
        blocked = phases[k]["blocked_by"][0]
        assert blocked["code"] == "prerequisite_stale", blocked
        assert reason in blocked["message"]


def _scenario(client, aid):
    return client.get(f"/api/assessments/{aid}/scenarios").json()[0]


def _pdf() -> bytes:
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Admins must use MFA.")
    return pdf.tobytes()


def _wait(client, tid, n=30):
    for _ in range(n):
        s = client.get(f"/api/tasks/{tid}").json()
        if s["status"] in {"done", "error"}:
            return s
        time.sleep(0.1)
    return s


# ---------- invalidation matrix ----------


def test_description_edit_stales_from_scenarios_and_reopens_scoping(client, fake):
    aid = _complete(client)
    r = client.post(f"/api/assessments/{aid}/description", json={"text": "A different service entirely."})
    assert r.status_code == 200
    phases = _phases(client, aid)
    # scoping is data-derived: force_continued is reset with the text.
    assert phases["scoping"]["state"] == "pending"
    with SessionLocal() as db:
        a = db.get(Assessment, aid)
        assert a.force_continued is False
        assert a.description.is_sufficient is False
    for k in UI[1:]:
        if k in UNSTAMPED:
            continue
        assert phases[k]["stale"]["reasons"] == ["Service description edited"], k
    assert phases["scenarios"]["ready"] is False
    assert phases["scenarios"]["blocked_by"][0]["code"] == "prerequisite_pending"
    r = client.post(f"/api/assessments/{aid}/scenarios/generate")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "prerequisite_pending"
    assert fake.calls == []


def test_description_unchanged_text_stamps_nothing(client, fake):
    aid = _complete(client)
    r = client.post(
        f"/api/assessments/{aid}/description",
        json={"text": "SaaS billing vendor processing EU PII via REST APIs."},
    )
    assert r.status_code == 200
    phases = _phases(client, aid)
    assert all(v["stale"] is None for v in phases.values())
    assert phases["scoping"]["state"] == "done"


def test_scoping_turn_stales_from_scenarios(client, fake):
    aid = _complete(client)
    fake.push_json(
        {
            "is_sufficient": True,
            "sufficiency_breakdown": {
                "data_types": 4, "hosting": 4, "network_access": 4, "identity_flow": 4,
                "regulatory_scope": 4, "geography": 4, "criticality": 4,
            },
            "missing_dimensions": [],
            "next_question": None,
            "summary_so_far": "ok",
        }
    )
    r = client.post(f"/api/assessments/{aid}/scoping/turn", json={"answer": "We also host in the US."})
    assert r.status_code == 200, r.text
    _wait(client, r.json()["task_id"])
    _assert_stale_from(_phases(client, aid), "scenarios", "Scoping answers changed")


def test_settings_standards_change_stales_from_scenarios(client, fake):
    aid = _complete(client)
    r = client.patch(
        f"/api/assessments/{aid}/settings",
        json={"standards_profile": {"required_attestations": ["SOC 2 Type 2"]}},
    )
    assert r.status_code == 200, r.text
    _assert_stale_from(_phases(client, aid), "scenarios", "Assessor standards changed")


def test_settings_date_change_stales_from_correlation_only(client, fake):
    aid = _complete(client)
    r = client.patch(f"/api/assessments/{aid}/settings", json={"as_of_date": "2026-01-15"})
    assert r.status_code == 200, r.text
    phases = _phases(client, aid)
    _assert_stale_from(phases, "correlation", "Analysis date changed (document extraction is not re-run)")
    # Evidence deliberately not invalidated (see patch_settings docstring).
    assert phases["evidence"]["state"] == "done" and phases["evidence"]["stale"] is None


def test_settings_unchanged_payload_stamps_nothing(client, fake):
    aid = _complete(client)
    client.patch(f"/api/assessments/{aid}/settings", json={"as_of_date": "2026-01-15"})
    # Re-running the stale steps is simulated by clearing the stamps.
    from app.tasks import mark_phase_done, mark_phase_started

    for key in ("cross_correlation", "gap_analysis", "narratives"):
        mark_phase_started(aid, key, None)
        mark_phase_done(aid, key)
    assert all(v["stale"] is None for v in _phases(client, aid).values())
    # The frontend resends the whole settings object: identical values → no stamp.
    r = client.patch(
        f"/api/assessments/{aid}/settings",
        json={"as_of_date": "2026-01-15", "standards_profile": {}},
    )
    assert r.status_code == 200
    assert all(v["stale"] is None for v in _phases(client, aid).values())


def test_settings_both_changed_stamps_both_reasons(client, fake):
    aid = _complete(client)
    r = client.patch(
        f"/api/assessments/{aid}/settings",
        json={"as_of_date": "2026-01-15", "standards_profile": {"mfa_policy": "MFA everywhere"}},
    )
    assert r.status_code == 200
    stale = _stale_map(_phases(client, aid))
    assert stale["scenarios"] == ["Assessor standards changed"]
    for k in ("correlation", "analysis", "score"):
        assert stale[k] == [
            "Assessor standards changed",
            "Analysis date changed (document extraction is not re-run)",
        ], k


def test_scenario_regeneration_stales_from_correlation(client, fake):
    aid = _complete(client)
    fake.push_json({"scenarios": [{"code": "DATA_LEAK", "name": "Data leakage", "description": "x",
                                   "inherent_impact": 3, "inherent_likelihood": 3}]})
    fake.push_json({"expected_controls": [{"code": "ENC.REST", "name": "Encryption at rest",
                                           "description": "", "weight": 1.0, "rationale": ""}]})
    r = client.post(f"/api/assessments/{aid}/scenarios/generate")
    assert r.status_code == 200, r.text
    assert _wait(client, r.json()["task_id"])["status"] == "done"
    phases = _phases(client, aid)
    # The re-run step itself is current again (mark_phase_started cleared it).
    assert phases["scenarios"]["state"] == "done" and phases["scenarios"]["stale"] is None
    _assert_stale_from(phases, "correlation", "Scenarios regenerated")


def test_document_upload_and_delete_stale_from_correlation(client, fake):
    aid = _complete(client)
    fake.push_json({"weaknesses": []})
    r = client.post(
        f"/api/assessments/{aid}/documents",
        data={"kind": "policy"},
        files={"file": ("mfa.pdf", io.BytesIO(_pdf()), "application/pdf")},
    )
    assert r.status_code == 201, r.text
    assert _wait(client, r.json()["weakness_task_id"])["status"] == "done"
    phases = _phases(client, aid)
    assert phases["evidence"]["state"] == "done"
    _assert_stale_from(phases, "correlation", "Document uploaded: mfa.pdf")

    r = client.delete(f"/api/documents/{r.json()['id']}")
    assert r.status_code in (200, 204), r.text
    stale = _stale_map(_phases(client, aid))
    for k in ("correlation", "analysis", "score"):
        assert stale[k] == ["Document uploaded: mfa.pdf", "Document deleted: mfa.pdf"], k


def test_retry_extraction_stales_from_correlation(client, fake):
    aid = _complete(client)
    with SessionLocal() as db:
        doc_id = db.query(Document).filter_by(assessment_id=aid).one().id
    fake.push_json({"weaknesses": []})
    r = client.post(f"/api/documents/{doc_id}/extract-weaknesses")
    assert r.status_code == 200, r.text
    assert _wait(client, r.json()["task_id"])["status"] == "done"
    _assert_stale_from(_phases(client, aid), "correlation", "Weaknesses re-extracted: policy.txt")


def test_correlation_rerun_stales_from_analysis(client, fake):
    aid = _complete(client)
    # No extracted weaknesses → the agent exits without an LLM call.
    r = client.post(f"/api/assessments/{aid}/cross-correlate")
    assert r.status_code == 200, r.text
    assert _wait(client, r.json()["task_id"])["status"] == "done"
    phases = _phases(client, aid)
    assert phases["correlation"]["stale"] is None
    _assert_stale_from(phases, "analysis", "Cross-correlation re-run")
    assert fake.calls == []


@pytest.mark.parametrize(
    "field, value, first_stale, reason",
    [
        ("name", "Renamed scenario", "analysis", "Scenario DATA_LEAK text edited"),
        ("description", "New description", "analysis", "Scenario DATA_LEAK text edited"),
        ("inherent_impact", 4, "score", "Scenario DATA_LEAK inherent rating edited"),
    ],
)
def test_scenario_patch(client, fake, field, value, first_stale, reason):
    aid = _complete(client)
    sc = _scenario(client, aid)
    r = client.patch(f"/api/scenarios/{sc['id']}", json={field: value})
    assert r.status_code == 200, r.text
    _assert_stale_from(_phases(client, aid), first_stale, reason)


def test_scenario_patch_without_change_stamps_nothing(client, fake):
    aid = _complete(client)
    sc = _scenario(client, aid)
    r = client.patch(f"/api/scenarios/{sc['id']}", json={"name": sc["name"], "inherent_impact": sc["inherent_impact"]})
    assert r.status_code == 200, r.text
    assert all(v["stale"] is None for v in _phases(client, aid).values())


def test_scenario_delete_stales_narratives(client, fake):
    aid = _complete(client)
    sc = _scenario(client, aid)
    r = client.delete(f"/api/scenarios/{sc['id']}")
    assert r.status_code in (200, 204), r.text
    phases = _phases(client, aid)
    assert phases["score"]["stale"]["reasons"] == [f"Scenario {sc['code']} deleted"]
    assert phases["analysis"]["stale"] is None


@pytest.mark.parametrize(
    "payload, first_stale, reason",
    [
        ({"name": "Encryption of data at rest"}, "analysis", "Control ENC.REST text edited"),
        ({"weight": 0.5}, "score", "Control ENC.REST weight edited"),
    ],
)
def test_expected_control_patch(client, fake, payload, first_stale, reason):
    aid = _complete(client)
    ec = _scenario(client, aid)["expected_controls"][0]
    r = client.patch(f"/api/expected-controls/{ec['id']}", json=payload)
    assert r.status_code == 200, r.text
    _assert_stale_from(_phases(client, aid), first_stale, reason)


def test_control_added_is_resume_safe(client, fake):
    aid = _complete(client)
    sc = _scenario(client, aid)
    r = client.post(f"/api/scenarios/{sc['id']}/expected-controls", json={"code": "IAM.MFA", "name": "MFA"})
    assert r.status_code in (200, 201), r.text
    phases = _phases(client, aid)
    _assert_stale_from(phases, "analysis", "Control IAM.MFA added to DATA_LEAK")
    assert phases["analysis"]["stale"]["resume_ok"] is True
    # `only_failed` targets the never-assessed control: allowed.
    fake.push_json({"controls": []})
    r = client.post(f"/api/assessments/{aid}/gap-analysis/run?only_failed=true")
    assert r.status_code == 200, r.text
    _wait(client, r.json()["task_id"])


def test_resume_refused_after_full_rerun_change(client, fake):
    aid = _complete(client)
    sc = _scenario(client, aid)
    r = client.post(f"/api/scenarios/{sc['id']}/expected-controls", json={"code": "IAM.MFA", "name": "MFA"})
    assert r.status_code in (200, 201)
    # Merge rule: resume_ok = old AND new; earliest `at` kept; reasons appended.
    first_at = _phases(client, aid)["analysis"]["stale"]["at"]
    r = client.patch(f"/api/scenarios/{sc['id']}", json={"name": "Renamed"})
    assert r.status_code == 200
    stale = _phases(client, aid)["analysis"]["stale"]
    assert stale["resume_ok"] is False
    assert stale["at"] == first_at
    assert stale["reasons"] == ["Control IAM.MFA added to DATA_LEAK", "Scenario DATA_LEAK text edited"]
    r = client.post(f"/api/assessments/{aid}/gap-analysis/run?only_failed=true")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "resume_requires_full_run"
    assert "Scenario DATA_LEAK text edited" in d["message"]
    ec = _scenario(client, aid)["expected_controls"][0]
    r = client.post(f"/api/expected-controls/{ec['id']}/assess-ai")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "resume_requires_full_run"
    assert fake.calls == []


def test_expected_control_delete_and_verdict_stale_narratives(client, fake):
    aid = _complete(client)
    ec = _scenario(client, aid)["expected_controls"][0]
    r = client.post(f"/api/expected-controls/{ec['id']}/assess", json={"coverage": "none", "effectiveness": "weak"})
    assert r.status_code == 200, r.text
    phases = _phases(client, aid)
    assert phases["analysis"]["stale"] is None
    _assert_stale_from(phases, "score", "Verdict for ENC.REST edited")
    ca_id = _scenario(client, aid)["expected_controls"][0]["assessment"]["id"]
    r = client.patch(f"/api/control-assessments/{ca_id}", json={"coverage": "partial"})
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/expected-controls/{ec['id']}")
    assert r.status_code in (200, 204), r.text
    stale = _phases(client, aid)["score"]["stale"]
    assert stale["reasons"] == ["Verdict for ENC.REST edited", "Control ENC.REST deleted from DATA_LEAK"]


def test_rerun_clears_only_its_own_stamp(client, fake):
    aid = _complete(client)
    client.patch(f"/api/assessments/{aid}/settings", json={"as_of_date": "2026-01-15"})
    r = client.post(f"/api/assessments/{aid}/cross-correlate")
    assert r.status_code == 200, r.text
    assert _wait(client, r.json()["task_id"])["status"] == "done"
    stale = _stale_map(_phases(client, aid))
    assert stale["correlation"] is None
    date_reason = "Analysis date changed (document extraction is not re-run)"
    assert stale["analysis"] == [date_reason, "Cross-correlation re-run"]
    assert stale["score"] == [date_reason, "Cross-correlation re-run"]
    phases = _phases(client, aid)
    assert phases["analysis"]["ready"] is True
    assert phases["score"]["ready"] is False
    assert phases["score"]["blocked_by"][0]["step"] == "analysis"


def test_reason_list_is_deduped_and_capped(client, fake):
    aid = _complete(client)
    with SessionLocal() as db:
        from app import workflow

        a = db.get(Assessment, aid)
        for i in range(7):
            workflow.invalidate_downstream(a, "narratives", reason=f"reason {i}")
        workflow.invalidate_downstream(a, "narratives", reason="reason 6")
        db.commit()
    stale = _phases(client, aid)["score"]["stale"]
    assert stale["reasons"] == [f"reason {i}" for i in range(2, 7)]


def test_pending_downstream_steps_are_not_stamped(client, fake):
    r = client.post("/api/assessments", json={"vendor_name": "Acme"})
    aid = r.json()["id"]
    advance_workflow(aid, "evidence")
    client.patch(f"/api/assessments/{aid}/settings", json={"standards_profile": {"mfa_policy": "x"}})
    phases = _phases(client, aid)
    assert phases["scenarios"]["stale"]["reasons"] == ["Assessor standards changed"]
    for k in ("correlation", "analysis", "score"):
        assert phases[k]["state"] == "pending" and phases[k]["stale"] is None, k


# ---------- evidence derivation ----------


def _add_doc(aid, filename, **fields) -> int:
    with SessionLocal() as db:
        d = Document(
            assessment_id=aid, kind="policy", filename=filename, mime="text/plain",
            sha256=filename.ljust(64, "0"), size_bytes=1, **fields,
        )
        db.add(d)
        db.commit()
        return d.id


def test_evidence_phase_derivation(client, fake):
    r = client.post("/api/assessments", json={"vendor_name": "Acme"})
    aid = r.json()["id"]
    advance_workflow(aid, "scenarios", with_document=False)
    assert _phases(client, aid)["evidence"]["state"] == "pending"

    now = datetime.utcnow()
    _add_doc(aid, "done.txt", parsed_at=now, weakness_extracted_at=now)
    ev = _phases(client, aid)["evidence"]
    assert ev["state"] == "done" and ev["ready"] is True

    live = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    running_id = _add_doc(aid, "running.txt", parsed_at=now, weakness_task_id=live)
    seed_running_task(aid, "document_extraction", live)
    ev = _phases(client, aid)["evidence"]
    assert ev["state"] == "running" and ev["task_id"] == live
    assert "1 of 2 documents extracted" in ev["detail"]
    docs = {d["filename"]: d for d in client.get(f"/api/assessments/{aid}/documents").json()}
    assert docs["running.txt"]["extraction_state"] == "running"
    assert docs["done.txt"]["extraction_state"] == "done"
    r = client.post(f"/api/assessments/{aid}/cross-correlate")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["code"] == "extraction_incomplete" and d["missing"][0]["document_ids"] == [running_id]

    with SessionLocal() as db:
        from app.models import TaskRecord

        db.get(TaskRecord, live).status = "error"
        db.commit()
    ev = _phases(client, aid)["evidence"]
    assert ev["state"] == "error" and ev["failed_targets"] == ["running.txt"]
    docs = {d["filename"]: d for d in client.get(f"/api/assessments/{aid}/documents").json()}
    assert docs["running.txt"]["extraction_state"] == "error" and docs["running.txt"]["weakness_error"]
    r = client.post(f"/api/assessments/{aid}/cross-correlate")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "extraction_failed"
    assert fake.calls == []


def test_reconcile_marks_interrupted_extraction(client, fake):
    from app.tasks import reconcile_interrupted_tasks

    r = client.post("/api/assessments", json={"vendor_name": "Acme"})
    aid = r.json()["id"]
    advance_workflow(aid, "scenarios", with_document=False)
    live = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    doc_id = _add_doc(aid, "cut.txt", parsed_at=datetime.utcnow(), weakness_task_id=live)
    seed_running_task(aid, "document_extraction", live)
    assert reconcile_interrupted_tasks() >= 1
    with SessionLocal() as db:
        d = db.get(Document, doc_id)
        assert d.weakness_task_id is None
        assert "interrupted" in (d.weakness_error or "")
    ev = _phases(client, aid)["evidence"]
    assert ev["state"] == "error" and ev["failed_targets"] == ["cut.txt"]
    doc = client.get(f"/api/assessments/{aid}/documents").json()[0]
    assert doc["extraction_state"] == "error" and "interrupted" in doc["weakness_error"]
