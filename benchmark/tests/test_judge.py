import json

import pytest
import respx
from httpx import Response

from bench.config import settings
from bench.judge import Judge, JudgeError

BASE = settings.OPENROUTER_BASE_URL.rstrip("/")

GOOD_MATCH = {
    "matches": [
        {"expected_id": "W1", "actual_id": 1, "confidence": "high",
         "justification": "'no mfa' vs 'MFA absent'"}
    ],
    "unmatched_expected": [{"expected_id": "W2", "justification": "not reported"}],
    "unmatched_actual": [{"actual_id": 2, "justification": "not in key"}],
}

EXPECTED = [
    {"id": "W1", "description": "no mfa", "severity": "high", "mapped_control_codes": []},
    {"id": "W2", "description": "no drp", "severity": "medium", "mapped_control_codes": []},
]
ACTUAL = [
    {"id": 1, "description": "MFA absent", "severity": "high", "quote": "", "mapped_control_codes": []},
    {"id": 2, "description": "weak logging", "severity": "low", "quote": "", "mapped_control_codes": []},
]


def _completion(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


@pytest.fixture
def judge(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "test-key")
    j = Judge(model="test/judge")
    yield j
    j.close()


@respx.mock
def test_match_happy_path(judge):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=Response(200, json=_completion(json.dumps(GOOD_MATCH)))
    )
    out, records = judge.match_weaknesses(EXPECTED, ACTUAL)
    assert len(out.matches) == 1
    assert out.matches[0].confidence == "high"
    assert len(records) == 1 and records[0].ok
    assert records[0].input_tokens == 100


@respx.mock
def test_match_fenced_json_accepted(judge):
    fenced = "```json\n" + json.dumps(GOOD_MATCH) + "\n```"
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=Response(200, json=_completion(fenced))
    )
    out, _ = judge.match_weaknesses(EXPECTED, ACTUAL)
    assert len(out.matches) == 1


@respx.mock
def test_match_retry_on_structural_failure(judge):
    # First answer drops actual id 2 → structural failure → retry succeeds
    bad = {**GOOD_MATCH, "unmatched_actual": []}
    route = respx.post(f"{BASE}/chat/completions")
    route.side_effect = [
        Response(200, json=_completion(json.dumps(bad))),
        Response(200, json=_completion(json.dumps(GOOD_MATCH))),
    ]
    out, records = judge.match_weaknesses(EXPECTED, ACTUAL)
    assert len(records) == 2
    assert not records[0].ok and "structural" in records[0].error
    assert records[1].ok
    assert len(out.unmatched_actual) == 1


@respx.mock
def test_match_fails_after_two_bad_answers(judge):
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=Response(200, json=_completion("not json at all"))
    )
    with pytest.raises(JudgeError) as ei:
        judge.match_weaknesses(EXPECTED, ACTUAL)
    # failed attempts are still available for persistence
    assert len(ei.value.records) == 2
    assert not any(r.ok for r in ei.value.records)


@respx.mock
def test_exec_rubric_happy_path(judge):
    rubric = {
        "coverage": [{"point_id": "K1", "status": "covered", "justification": "verdict present"}],
        "violations": [{"claim_id": "F1", "status": "clean", "justification": "not asserted"}],
        "faithfulness": {"score": 4, "unsupported_claims": [], "justification": "traceable"},
    }
    respx.post(f"{BASE}/chat/completions").mock(
        return_value=Response(200, json=_completion(json.dumps(rubric)))
    )
    out, records = judge.grade_exec_summary(
        "Verdict: Moderate", "digest", [{"id": "K1", "point": "verdict"}],
        [{"id": "F1", "claim": "ISO certified"}],
    )
    assert out.faithfulness.score == 4
    assert records[0].purpose == "exec_rubric"
