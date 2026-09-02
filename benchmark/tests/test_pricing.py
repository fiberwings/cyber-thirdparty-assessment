"""List-price estimation for judge calls recorded before metered cost capture."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from bench.config import settings
from bench.judge import Judge, _usage_cost
from bench.pricing import MODELS_URL, Price, estimate, fetch_pricing


_EXPECTED = [{"id": "W1", "description": "no mfa", "severity": "high", "mapped_control_codes": []}]
_ACTUAL = [{"id": 1, "description": "MFA absent", "severity": "high",
            "quote": "", "mapped_control_codes": []}]
_ANSWER = {
    "matches": [{"expected_id": "W1", "actual_id": 1, "confidence": "high",
                 "justification": "same finding"}],
    "unmatched_expected": [],
    "unmatched_actual": [],
}


@pytest.fixture
def judge(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "test-key")
    j = Judge(model="test/judge")
    yield j
    j.close()


@respx.mock
def test_fetch_pricing_parses_and_skips_malformed():
    respx.get(MODELS_URL).mock(return_value=httpx.Response(200, json={"data": [
        {"id": "a/ok", "pricing": {"prompt": "0.000001", "completion": "0.000004"}},
        {"id": "a/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "a/bad", "pricing": {"prompt": "not-a-number", "completion": "1"}},
    ]}))
    p = fetch_pricing()
    assert p["a/ok"] == Price(1e-6, 4e-6)
    assert p["a/free"] == Price(0.0, 0.0)
    assert "a/bad" not in p


def test_estimate_returns_none_for_unlisted_model():
    pricing = {"a/ok": Price(1e-6, 4e-6)}
    assert estimate("a/ok", 1000, 500, pricing) == pytest.approx(0.003)
    assert estimate("a/ok", None, None, pricing) == 0.0
    # A delisted model has an unknown price — never silently $0.
    assert estimate("a/gone", 1000, 500, pricing) is None


def test_usage_cost_from_openrouter_block():
    assert _usage_cost({
        "cost": 0.0042,
        "prompt_tokens_details": {"cached_tokens": 128},
        "completion_tokens_details": {"reasoning_tokens": 27},
    }) == {"cached_tokens": 128, "reasoning_tokens": 27,
           "cost_usd": 0.0042, "cost_source": "openrouter"}


def test_usage_cost_without_cost_stays_unpriced():
    out = _usage_cost({"prompt_tokens": 10})
    assert out["cost_usd"] is None and out["cost_source"] == ""
    assert out["cached_tokens"] is None and out["reasoning_tokens"] is None
    # An explicit zero is a real (free) price, not a missing one.
    assert _usage_cost({"cost": 0})["cost_source"] == "openrouter"


def test_judge_records_cost_end_to_end(judge):
    """The Judge client carries usage cost/cache detail onto the call record."""
    payload = {
        "choices": [{"message": {"content": json.dumps(
            _ANSWER)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.0009,
                  "prompt_tokens_details": {"cached_tokens": 60},
                  "completion_tokens_details": {"reasoning_tokens": 5}},
    }
    with respx.mock:
        respx.post(f"{settings.OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json=payload)
        )
        _, records = judge.match_weaknesses(_EXPECTED, _ACTUAL)
    assert len(records) == 1
    r = records[0]
    assert r.cost_usd == pytest.approx(0.0009) and r.cost_source == "openrouter"
    assert (r.cached_tokens, r.reasoning_tokens) == (60, 5)


def test_judge_records_no_cost_when_unreported(judge):
    payload = {
        "choices": [{"message": {"content": json.dumps(
            _ANSWER)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }
    with respx.mock:
        respx.post(f"{settings.OPENROUTER_BASE_URL}/chat/completions").mock(
            return_value=httpx.Response(200, json=payload)
        )
        _, records = judge.match_weaknesses(_EXPECTED, _ACTUAL)
    assert records[0].cost_usd is None and records[0].cost_source == ""
