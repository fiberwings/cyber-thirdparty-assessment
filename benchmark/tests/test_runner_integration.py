"""Full orchestration test against a stubbed backend + stubbed judge.

Verifies the endpoint sequence (scenarios before documents), grading and
persistence — zero LLM cost, no main-app import.
"""

import json

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from bench.cases import load_case
from bench.config import settings
from bench.db import get_session
from bench.models import CaseResult, FindingMatch, JudgeCall, Run
from bench.runner import RunConfig, run_batch

BACKEND = "http://stub-backend:8000"
OPENROUTER = settings.OPENROUTER_BASE_URL.rstrip("/")

CASE_YAML = """\
id: stubcase
vendor_name: "Stub Vendor"
description: "Stub vendor processing employee PII in AWS, SAML SSO, payroll-critical."
documents:
  - path: docs/policy.pdf
    kind: policy
golden:
  expected_weaknesses:
    - id: S-W1
      description: "no mfa for admins"
      severity: high
    - id: S-W2
      description: "backups never restore-tested"
      severity: medium
  exec_summary_rubric:
    must_cover:
      - id: S-K1
        point: "verdict stated"
    must_not_claim:
      - id: S-F1
        claim: "ISO certified"
"""

REPORT = {
    "assessment": {"id": 1, "vendor_name": "Stub Vendor"},
    "aggregate": {"band": "Moderate", "rank": 2, "weighted_mean_rank": 2.0, "top2_mean_rank": 2.0},
    "scenarios": [
        {"code": "DATA_LEAK", "name": "PII leak", "band": "Moderate",
         "residual_impact": 3, "residual_likelihood": 2}
    ],
    "weaknesses": [
        {"id": 11, "severity": "high", "description": "MFA absent for admin accounts",
         "quote": "", "mapped_control_codes": [], "unmatched": False},
        {"id": 12, "severity": "low", "description": "logging gaps",
         "quote": "", "mapped_control_codes": [], "unmatched": True},
    ],
    "meta_issues": [],
    "executive_summary": {
        "generated_at": "2026-07-06T00:00:00Z", "model_id": "stub/reasoner", "stale": False,
        "verdict": "Moderate residual risk",
        "key_risks": [{"title": "Admin MFA gap", "why_it_matters": "credential theft",
                       "scenario_codes": [], "weakness_ids": [11], "evidence_basis": "policy"}],
        "limitations": [], "recommended_actions": [],
    },
}

JUDGE_MATCH = {
    "matches": [{"expected_id": "S-W1", "actual_id": 11, "confidence": "high",
                 "justification": "'no mfa for admins' vs 'MFA absent for admin accounts'"}],
    "unmatched_expected": [{"expected_id": "S-W2", "justification": "not reported"}],
    "unmatched_actual": [{"actual_id": 12, "justification": "not in key"}],
}

JUDGE_RUBRIC = {
    "coverage": [{"point_id": "S-K1", "status": "covered", "justification": "verdict present"}],
    "violations": [{"claim_id": "S-F1", "status": "clean", "justification": "not asserted"}],
    "faithfulness": {"score": 5, "unsupported_claims": [], "justification": "traceable"},
}


@pytest.fixture
def stub_case(tmp_path):
    case_dir = tmp_path / "stubcase"
    (case_dir / "docs").mkdir(parents=True)
    (case_dir / "case.yaml").write_text(CASE_YAML)
    (case_dir / "docs" / "policy.pdf").write_bytes(b"%PDF-1.4 fake")
    return load_case(case_dir)


def _mock_backend(deleted: list, fail_gap: bool = False):
    if fail_gap:  # must register before the generic /api/tasks route (respx matches in order)
        respx.get(f"{BACKEND}/api/tasks/t-gap").mock(
            return_value=Response(200, json={"task_id": "t-gap", "status": "error",
                                             "progress": 0.3, "detail": "reasoner exploded"}))
    respx.get(f"{BACKEND}/api/health").mock(return_value=Response(200, json={"ok": True}))
    respx.get(f"{BACKEND}/api/models").mock(return_value=Response(200, json=[
        {"name": "fast", "default_model": "stub/fast", "alternatives": []},
        {"name": "reasoner", "default_model": "stub/reasoner", "alternatives": []},
    ]))
    respx.post(f"{BACKEND}/api/assessments").mock(
        return_value=Response(201, json={"id": 1, "vendor_name": "Stub Vendor"}))
    respx.post(f"{BACKEND}/api/assessments/1/description").mock(
        return_value=Response(200, json={"text": "ok"}))
    respx.post(f"{BACKEND}/api/assessments/1/scoping/force-continue").mock(
        return_value=Response(200, json={"ok": True}))
    respx.post(f"{BACKEND}/api/assessments/1/scenarios/generate").mock(
        return_value=Response(200, json={"task_id": "t-scen"}))
    respx.post(f"{BACKEND}/api/assessments/1/documents").mock(
        return_value=Response(201, json={"id": 5, "filename": "policy.pdf",
                                         "weakness_task_id": "t-extract"}))
    respx.post(f"{BACKEND}/api/assessments/1/cross-correlate").mock(
        return_value=Response(200, json={"task_id": "t-corr"}))
    respx.post(f"{BACKEND}/api/assessments/1/gap-analysis/run").mock(
        return_value=Response(200, json={"task_id": "t-gap"}))
    respx.post(f"{BACKEND}/api/assessments/1/recalculate").mock(
        return_value=Response(200, json={"scenarios": [], "aggregate": REPORT["aggregate"]}))
    respx.post(f"{BACKEND}/api/assessments/1/narratives/run").mock(
        return_value=Response(200, json={"task_id": "t-narr"}))
    respx.get(f"{BACKEND}/api/assessments/1/report").mock(
        return_value=Response(200, json=REPORT))
    respx.get(url__regex=rf"{BACKEND}/api/tasks/.*").mock(
        return_value=Response(200, json={"task_id": "t", "status": "done",
                                         "progress": 1.0, "detail": ""}))

    def _delete(request):
        deleted.append(str(request.url))
        return Response(204)

    respx.delete(f"{BACKEND}/api/assessments/1").mock(side_effect=_delete)


def _mock_judge():
    def _judge_response(request):
        body = json.loads(request.content)
        text = body["messages"][0]["content"]
        payload = JUDGE_MATCH if "answer key" in text else JUDGE_RUBRIC
        return Response(200, json={
            "choices": [{"message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        })

    respx.post(f"{OPENROUTER}/chat/completions").mock(side_effect=_judge_response)


@respx.mock
def test_full_batch(fresh_db, stub_case, monkeypatch):
    monkeypatch.setattr(settings, "BENCH_BACKEND_URL", BACKEND)
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "MAIN_DB_PATH", "/nonexistent/tprm.sqlite")
    monkeypatch.setattr(settings, "POLL_INTERVAL", 0.01)

    deleted: list = []
    _mock_backend(deleted)
    _mock_judge()

    run_id, status = run_batch([stub_case], RunConfig(cleanup="ok"))
    assert status == "done"

    # Ordering: scenarios/generate must precede the document upload
    paths = [str(c.request.url.path) for c in respx.calls]
    assert paths.index("/api/assessments/1/scenarios/generate") < paths.index(
        "/api/assessments/1/documents"
    )
    # Cleanup happened (status ok + cleanup="ok")
    assert deleted

    session = get_session()
    run = session.get(Run, run_id)
    assert run.status == "done"
    assert run.judge_model == settings.JUDGE_MODEL
    assert json.loads(run.models_json)["profiles"]

    cr = session.scalars(select(CaseResult).where(CaseResult.run_id == run_id)).one()
    assert cr.status == "ok"
    assert (cr.tp, cr.fp, cr.fn) == (1, 1, 1)
    assert cr.f1 == 0.5
    assert cr.exec_overall == 100.0
    assert cr.aggregate_band == "Moderate"
    assert cr.assessment_deleted

    matches = session.scalars(
        select(FindingMatch).where(FindingMatch.case_result_id == cr.id)
    ).all()
    assert {m.match_type for m in matches} == {"matched", "missed", "extra"}

    calls = session.scalars(
        select(JudgeCall).where(JudgeCall.case_result_id == cr.id)
    ).all()
    assert {c.purpose for c in calls} == {"weakness_match", "exec_rubric"}
    assert all(c.ok for c in calls)
    session.close()


@respx.mock
def test_stage_error_recorded_batch_continues(fresh_db, stub_case, monkeypatch):
    monkeypatch.setattr(settings, "BENCH_BACKEND_URL", BACKEND)
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(settings, "MAIN_DB_PATH", "/nonexistent/tprm.sqlite")
    monkeypatch.setattr(settings, "POLL_INTERVAL", 0.01)

    deleted: list = []
    _mock_backend(deleted, fail_gap=True)
    _mock_judge()

    run_id, status = run_batch([stub_case], RunConfig(cleanup="ok"))
    assert status == "failed"

    session = get_session()
    cr = session.scalars(select(CaseResult).where(CaseResult.run_id == run_id)).one()
    assert cr.status == "error"
    assert cr.error_stage == "gap_analysis"
    assert "reasoner exploded" in cr.error_detail
    # errored assessments are kept for debugging under cleanup="ok"
    assert not cr.assessment_deleted and not deleted
    session.close()
