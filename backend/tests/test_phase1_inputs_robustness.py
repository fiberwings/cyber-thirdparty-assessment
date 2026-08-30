"""Accuracy program Phase 1 (R7 inputs, R8 robustness)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.ai.agents import document_weaknesses, gap_analysis
from app.ai.router import call_structured
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Assessment,
    Chunk,
    ControlAssessment,
    Document,
    ExpectedControl,
    ModelCall,
    Scenario,
    TaskRecord,
)
from app.schemas.ai import DocumentWeaknessListOut
from app.tasks import reconcile_interrupted_tasks, registry

GOOD_CONTROL = {
    "control_code": "IAM.MFA",
    "coverage": "full",
    "effectiveness": "strong",
    "citations": [{"document_id": 1, "page": 7, "section_path": "CC6.1",
                   "quote": "All administrative users must authenticate with MFA."}],
    "rationale": "MFA is mandatory per CC6.1.",
    "meta_flags": [],
}


def _two_control_fixture(db, *, as_of=None, profile=None):
    a = Assessment(vendor_name="Acme", as_of_date=as_of, standards_profile=profile or {})
    db.add(a)
    db.flush()
    s = Scenario(assessment_id=a.id, code="DATA_LEAK", name="x", description="x",
                 inherent_impact=3, inherent_likelihood=3)
    db.add(s)
    db.flush()
    ec1 = ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA")
    ec2 = ExpectedControl(scenario_id=s.id, code="LOG.SIEM", name="SIEM")
    db.add_all([ec1, ec2])
    doc = Document(assessment_id=a.id, kind="soc", filename="soc.pdf",
                   mime="application/pdf", sha256="abc", size_bytes=100)
    db.add(doc)
    db.flush()
    db.add(Chunk(document_id=doc.id, page=7, section_path="CC6.1", ord=1,
                 text="All administrative users must authenticate with MFA."))
    db.commit()
    for o in (a, s, ec1, ec2):
        db.refresh(o)
    return a, s, ec1, ec2


# ---------------- R8: per-control failures are persisted, phase completes ----------------

@pytest.fixture()
def retrieval_mode(monkeypatch):
    """Force the per-control FTS path (whole-bundle mode is Phase 2's default)."""
    monkeypatch.setattr(gap_analysis, "WHOLE_BUNDLE_MAX_TOKENS", 0)


@pytest.mark.asyncio
async def test_run_full_partial_failure_is_persisted_and_resumable(fresh_db, fake_client, retrieval_mode):
    # One good response only: whichever control gets it succeeds, the other
    # runs out of canned responses (AssertionError) → recorded, not raised.
    fake_client.push_json(GOOD_CONTROL)
    with SessionLocal() as db:
        a, s, ec1, ec2 = _two_control_fixture(db)
        result = await gap_analysis.run_full(db, a, client=fake_client)
        assert result.total == 2 and len(result.failed) == 1
        assert "1 failed and can be re-run" in result.warning
        db.expire_all()
        db.refresh(a)
        failed = gap_analysis.failed_targets(a)
        assert len(failed) == 1 and failed[0].startswith("DATA_LEAK/")
        cas = {ec.code: ec.assessment for ec in s.expected_controls}
        ok_code = [c for c, ca in cas.items() if ca is not None and not ca.last_error][0]
        bad_code = [c for c, ca in cas.items() if ca is not None and ca.last_error][0]
        assert cas[ok_code].coverage == "full" and cas[ok_code].last_run_at is not None
        # failed control: scored exactly like "unassessed" (none/unknown)
        assert cas[bad_code].coverage == "none" and cas[bad_code].effectiveness == "unknown"
        assert "AssertionError" in cas[bad_code].last_error

        # resume: only the failed control is re-run
        fake_client.push_json({**GOOD_CONTROL, "control_code": bad_code})
        result2 = await gap_analysis.run_full(db, a, client=fake_client, only_failed=True)
        assert result2.total == 1 and not result2.failed and result2.warning is None
        db.expire_all()
        db.refresh(a)
        assert gap_analysis.failed_targets(a) == []
        # 1 ok + 1 failed attempt in the first run, 1 in the resume
        assert len(fake_client.calls) == 3


@pytest.mark.asyncio
async def test_run_full_all_failed_still_raises(fresh_db, fake_client, retrieval_mode):
    with SessionLocal() as db:
        a, *_ = _two_control_fixture(db)
        with pytest.raises(Exception, match="0/2 controls assessed"):
            await gap_analysis.run_full(db, a, client=fake_client)
        db.expire_all()
        db.refresh(a)
        assert len(gap_analysis.failed_targets(a)) == 2


# ---------------- R7: analysis date + standards in prompts ----------------

@pytest.mark.asyncio
async def test_gap_analysis_prompt_carries_as_of_date_and_standards(fresh_db, fake_client):
    fake_client.push_json(GOOD_CONTROL)
    profile = {"policy_review_months": 12, "required_attestations": ["SOC 2 Type 2"],
               "allowed_residency": ["EEA"], "vuln_remediation_sla": {"critical_days": 14}}
    with SessionLocal() as db:
        a, s, ec1, _ = _two_control_fixture(db, as_of="2026-05-01", profile=profile)
        await gap_analysis.assess_control(db, a, s, ec1, client=fake_client)
    user = fake_client.calls[0]["messages"][1]["content"]
    assert "# Analysis date: 2026-05-01" in user
    assert "Required attestations: SOC 2 Type 2" in user
    assert "reviewed at least every 12 months" in user
    assert "stored / processed in: EEA" in user
    assert "critical ≤ 14 days" in user


@pytest.mark.asyncio
async def test_gap_analysis_prompt_without_profile_says_so(fresh_db, fake_client):
    fake_client.push_json(GOOD_CONTROL)
    with SessionLocal() as db:
        a, s, ec1, _ = _two_control_fixture(db)
        await gap_analysis.assess_control(db, a, s, ec1, client=fake_client)
    user = fake_client.calls[0]["messages"][1]["content"]
    assert "no assessor standards supplied" in user


@pytest.mark.asyncio
async def test_document_extraction_uses_as_of_date_not_wall_clock(fresh_db, fake_client):
    fake_client.push_json({"weaknesses": []})
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme", as_of_date="2026-05-01")
        db.add(a)
        db.flush()
        doc = Document(assessment_id=a.id, kind="policy", filename="p.pdf",
                       mime="application/pdf", sha256="x", size_bytes=1)
        db.add(doc)
        db.flush()
        db.add(Chunk(document_id=doc.id, page=1, section_path="1", ord=0, text="Policy v1 2024."))
        db.commit()
        await document_weaknesses.extract(db, doc.id, client=fake_client)
    user = fake_client.calls[0]["messages"][1]["content"]
    assert user.startswith("# Analysis date: 2026-05-01\n")
    # upload happened "today" (2026-08-30 or later), after the pinned analysis
    # date → the upload date is withheld entirely (run artefact, not evidence)
    assert "Document uploaded" not in user
    assert "judge staleness ONLY against the analysis date" in user
    assert "Assessor standards" in user


# ---------------- R8: long questionnaires → windows, no cap ----------------

@pytest.mark.asyncio
async def test_questionnaire_falls_back_to_windows_without_cap(fresh_db, fake_client, monkeypatch):
    monkeypatch.setattr(document_weaknesses, "QUESTIONNAIRE_WINDOW_MAX_TOKENS", 40)
    # single call truncated twice (router doubles the budget once, then gives up)
    fake_client.push_truncated("")
    fake_client.push_truncated("")
    row = {"severity": "medium", "description": "MFA is not enforced.", "quote": "Q: MFA? A: No",
           "section_path": "Sheet 'A' (rows 1-2)", "page": None,
           "kind_signal": "questionnaire_negative", "suggested_control_codes": []}
    # three windows → three direct extraction calls
    fake_client.push_json({"weaknesses": [row]})
    fake_client.push_json({"weaknesses": [{**row, "quote": "Q: SIEM? A: No", "section_path": "Sheet 'B' (rows 1-2)"}]})
    fake_client.push_json({"weaknesses": [{**row, "quote": "Q: DR? A: No", "section_path": "Sheet 'C' (rows 1-2)"}]})
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.flush()
        doc = Document(assessment_id=a.id, kind="questionnaire", filename="sig.xlsx",
                       mime="application/x", sha256="x", size_bytes=1)
        db.add(doc)
        db.flush()
        for i, sheet in enumerate("ABC"):
            db.add(Chunk(document_id=doc.id, page=None, section_path=f"Sheet '{sheet}' (rows 1-2)",
                         ord=i, text=f"Q: control {sheet}? A: No. " * 8))
        db.commit()
        n = await document_weaknesses.extract(db, doc.id, client=fake_client)
        assert n == 3
        purposes = [m.purpose for m in db.query(ModelCall).order_by(ModelCall.id).all()]
        assert purposes == ["document_weakness_extract"] + ["document_weakness_extract_window"] * 3
    windows = [c["messages"][1]["content"] for c in fake_client.calls[2:]]
    assert all("Window" in w and "report only what is in this window" in w for w in windows)
    assert not hasattr(document_weaknesses, "PER_DOC_PHASE2_CAP")


# ---------------- dev-only LLM cache ----------------

@pytest.mark.asyncio
async def test_dev_cache_off_by_default_and_refused_in_production(fresh_db, fake_client, monkeypatch):
    assert settings.llm_dev_cache is False and settings.llm_dev_cache_active is False
    monkeypatch.setattr(settings, "llm_dev_cache", True)
    monkeypatch.setattr(settings, "app_env", "production")
    assert settings.llm_dev_cache_active is False
    msgs = [{"role": "user", "content": "x"}]
    fake_client.push_json({"weaknesses": []})
    fake_client.push_json({"weaknesses": []})
    with SessionLocal() as db:
        await call_structured(db, purpose="t", profile="fast", messages=msgs,
                              schema=DocumentWeaknessListOut, client=fake_client)
        await call_structured(db, purpose="t", profile="fast", messages=msgs,
                              schema=DocumentWeaknessListOut, client=fake_client)
    assert len(fake_client.calls) == 2  # no caching in production


@pytest.mark.asyncio
async def test_dev_cache_serves_second_identical_call(fresh_db, fake_client, monkeypatch):
    monkeypatch.setattr(settings, "llm_dev_cache", True)
    monkeypatch.setattr(settings, "app_env", "dev")
    msgs = [{"role": "user", "content": "x"}]
    fake_client.push_json({"weaknesses": []})
    with SessionLocal() as db:
        await call_structured(db, purpose="t", profile="fast", messages=msgs,
                              schema=DocumentWeaknessListOut, client=fake_client)
        await call_structured(db, purpose="t", profile="fast", messages=msgs,
                              schema=DocumentWeaknessListOut, client=fake_client)
        # different sampling params → different key → real call (queue empty → error)
        fake_client.push_json({"weaknesses": []})
        await call_structured(db, purpose="t", profile="fast", messages=msgs, max_tokens=4096,
                              schema=DocumentWeaknessListOut, client=fake_client)
        rows = db.query(ModelCall).order_by(ModelCall.id).all()
    assert len(fake_client.calls) == 2
    assert [r.cached for r in rows] == [False, True, False]
    assert rows[1].input_tokens == 0 and rows[1].output_tokens == 0


# ---------------- durable task registry ----------------

def test_interrupted_tasks_reconciled_on_startup(fresh_db):
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme",
                       phase_state={"gap_analysis": {"started_at": "x", "task_id": "t-1", "error": None}})
        db.add(a)
        db.add(TaskRecord(id="t-1", status="running", kind="gap_analysis", assessment_id=1))
        db.commit()
    assert reconcile_interrupted_tasks() == 1
    snap = registry.get("t-1")
    assert snap is not None and snap.status == "error" and "restart" in snap.error
    with SessionLocal() as db:
        a = db.get(Assessment, 1)
        assert a.phase_state["gap_analysis"]["task_id"] is None
        assert "restart" in a.phase_state["gap_analysis"]["error"]
    assert registry.get("nope") is None


# ---------------- API surface ----------------

@pytest.fixture()
def client(monkeypatch, fresh_db, fake_client):
    from app import main as main_mod
    from app.ai import router as router_mod
    monkeypatch.setattr(router_mod, "OpenRouterClient", lambda *a, **kw: fake_client)
    with TestClient(main_mod.app) as c:
        yield c


def test_settings_endpoint_and_per_control_rerun(client, fake_client):
    r = client.post("/api/assessments", json={"vendor_name": "Acme"})
    aid = r.json()["id"]
    assert r.json()["as_of_date_set"] is False

    r = client.patch(f"/api/assessments/{aid}/settings",
                     json={"as_of_date": "2026-05-01",
                           "standards_profile": {"policy_review_months": 12}})
    assert r.status_code == 200
    body = r.json()
    assert body["as_of_date"] == "2026-05-01" and body["as_of_date_set"] is True
    assert body["standards_profile"]["policy_review_months"] == 12
    assert body["standards_profile"]["required_attestations"] == []

    r = client.patch(f"/api/assessments/{aid}/settings", json={"clear_as_of_date": True})
    assert r.json()["as_of_date_set"] is False

    with SessionLocal() as db:
        a = db.get(Assessment, aid)
        s = Scenario(assessment_id=a.id, code="DATA_LEAK", name="x", description="x",
                     inherent_impact=3, inherent_likelihood=3)
        db.add(s)
        db.flush()
        ec = ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA")
        db.add(ec)
        db.add(ControlAssessment(expected_control_id=None))  # placeholder removed below
        db.rollback()
        db.add(s); db.flush(); ec = ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA"); db.add(ec)
        doc = Document(assessment_id=a.id, kind="soc", filename="soc.pdf",
                       mime="application/pdf", sha256="abc", size_bytes=100)
        db.add(doc)
        db.flush()
        db.add(Chunk(document_id=doc.id, page=7, section_path="CC6.1", ord=1,
                     text="All administrative users must authenticate with MFA."))
        db.commit()
        ec_id = ec.id

    # failed run: no canned response → per-control error, phase done with warning
    r = client.post(f"/api/assessments/{aid}/gap-analysis/run")
    tid = r.json()["task_id"]
    st = client.get(f"/api/tasks/{tid}").json()
    assert st["status"] == "error"  # every control failed → run-level failure
    a = client.get(f"/api/assessments/{aid}").json()
    assert a["phases"]["analysis"]["state"] == "error"

    # per-control AI re-run succeeds (whole-bundle mode → batched shape)
    fake_client.push_json({"controls": [GOOD_CONTROL]})
    r = client.post(f"/api/expected-controls/{ec_id}/assess-ai")
    tid = r.json()["task_id"]
    st = client.get(f"/api/tasks/{tid}").json()
    assert st["status"] == "done", st
    sc = client.get(f"/api/expected-controls/{ec_id}/scenario").json()
    ca = sc["expected_controls"][0]["assessment"]
    assert ca["coverage"] == "full" and ca["last_error"] is None and ca["last_run_at"]

    # durable task row exists
    with SessionLocal() as db:
        assert db.get(TaskRecord, tid).status == "done"
