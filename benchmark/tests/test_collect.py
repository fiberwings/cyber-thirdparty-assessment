"""collect_model_calls: read-only aggregation of the app's model_call table."""

from __future__ import annotations

import sqlite3

import pytest

from bench.collect import collect_model_calls

_FULL_DDL = (
    "CREATE TABLE model_call (id INTEGER PRIMARY KEY, assessment_id INTEGER, purpose TEXT, "
    "model_id TEXT, latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
    "cached_tokens INTEGER, reasoning_tokens INTEGER, cost_usd FLOAT, cost_source TEXT, "
    "ok BOOLEAN, cached BOOLEAN)"
)
_OLD_DDL = (
    "CREATE TABLE model_call (id INTEGER PRIMARY KEY, assessment_id INTEGER, purpose TEXT, "
    "model_id TEXT, latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, ok BOOLEAN)"
)


def _db(tmp_path, ddl, rows):
    path = tmp_path / "tprm.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(ddl)
    for r in rows:
        cols = ", ".join(r)
        conn.execute(f"INSERT INTO model_call ({cols}) VALUES ({', '.join('?' * len(r))})", list(r.values()))
    conn.commit()
    conn.close()
    return str(path)


def test_aggregates_cost_and_cache_tokens_by_purpose(tmp_path):
    rows = [
        dict(assessment_id=1, purpose="p1", model_id="m", latency_ms=100, input_tokens=1000,
             output_tokens=200, cached_tokens=600, reasoning_tokens=50, cost_usd=0.01,
             cost_source="openrouter", ok=1, cached=0),
        # a live retry of the same purpose, billed
        dict(assessment_id=1, purpose="p1", model_id="m", latency_ms=50, input_tokens=1000,
             output_tokens=100, cached_tokens=900, reasoning_tokens=0, cost_usd=0.005,
             cost_source="estimated", ok=1, cached=0),
        # dev-cache hit: zero everything, must not count as cost_missing
        dict(assessment_id=1, purpose="p2", model_id="m", latency_ms=1, input_tokens=0,
             output_tokens=0, cached_tokens=0, reasoning_tokens=0, cost_usd=0.0, ok=1, cached=1),
        # live, ok, but OpenRouter reported no cost → flagged
        dict(assessment_id=1, purpose="p2", model_id="m", latency_ms=80, input_tokens=500,
             output_tokens=50, cached_tokens=0, reasoning_tokens=0, cost_usd=0.0, ok=1, cached=0),
        # failed call: an error, not a missing cost
        dict(assessment_id=1, purpose="p2", model_id="m", latency_ms=80, input_tokens=0,
             output_tokens=0, cached_tokens=0, reasoning_tokens=0, cost_usd=0.0, ok=0, cached=0),
        # another assessment — excluded
        dict(assessment_id=2, purpose="p1", model_id="m", latency_ms=1, input_tokens=9,
             output_tokens=9, cached_tokens=9, reasoning_tokens=9, cost_usd=9.0, ok=1, cached=0),
    ]
    out = collect_model_calls(1, _db(tmp_path, _FULL_DDL, rows))
    by = {r["purpose"]: r for r in out["by_purpose"]}
    assert by["p1"]["calls"] == 2
    assert (by["p1"]["input_tokens"], by["p1"]["output_tokens"]) == (2000, 300)
    assert (by["p1"]["cached_tokens"], by["p1"]["reasoning_tokens"]) == (1500, 50)
    assert by["p1"]["cost_usd"] == pytest.approx(0.015)
    assert by["p1"]["cost_missing"] == 0 and by["p1"]["errors"] == 0
    assert by["p1"]["cost_estimated"] == 1 and by["p2"]["cost_estimated"] == 0
    assert by["p2"]["calls"] == 3 and by["p2"]["errors"] == 1
    assert by["p2"]["cost_missing"] == 1
    assert out["total_calls"] == 5
    assert out["total_cost_usd"] == pytest.approx(0.015)
    assert out["total_cached_tokens"] == 1500 and out["total_reasoning_tokens"] == 50
    assert out["total_cost_missing"] == 1 and out["total_cost_estimated"] == 1


def test_older_app_db_without_new_columns_still_yields_tokens(tmp_path):
    rows = [dict(assessment_id=1, purpose="p1", model_id="m", latency_ms=10,
                 input_tokens=100, output_tokens=20, ok=1)]
    out = collect_model_calls(1, _db(tmp_path, _OLD_DDL, rows))
    assert out is not None
    p = out["by_purpose"][0]
    assert (p["input_tokens"], p["output_tokens"]) == (100, 20)
    assert (p["cached_tokens"], p["reasoning_tokens"], p["cost_usd"]) == (0, 0, 0)
    # No cost column at all → every live ok call is a missing cost, not a free one.
    assert p["cost_missing"] == 1 and out["total_cost_missing"] == 1
    assert p["cost_estimated"] == 0 and out["total_cost_estimated"] == 0


def test_missing_db_or_table_is_none(tmp_path):
    assert collect_model_calls(1, str(tmp_path / "nope.sqlite")) is None
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()
    assert collect_model_calls(1, str(path)) is None


def test_sum_or_none_distinguishes_unpriced_from_free():
    from dashboard.app import _sum_or_none

    assert _sum_or_none([]) is None
    assert _sum_or_none([None, None]) is None  # unpriced, not $0
    assert _sum_or_none([0.5, None, 0.25]) == pytest.approx(0.75)
