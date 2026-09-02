"""Per-call cost and provider-cache token capture on ModelCall.

OpenRouter's usage accounting is always on: `usage.cost` (USD credits),
`usage.prompt_tokens_details.cached_tokens` and
`usage.completion_tokens_details.reasoning_tokens` ride along with every live
response. They accumulate across retries (each attempt was billed), stay at
zero on dev-cache hits, and a live response *without* a cost is logged loudly
rather than silently recorded as free.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from pydantic import BaseModel

import sys
from pathlib import Path

from app.ai.router import _UsageTally, call_structured, call_text
from app.db import SessionLocal
from app.models import ModelCall


class _Out(BaseModel):
    control_code: str
    coverage: str
    effectiveness: str
    rationale: str


MESSAGES = [{"role": "user", "content": "assess"}]
VALID = {"control_code": "X", "coverage": "full", "effectiveness": "strong", "rationale": "ok"}


def test_tally_reads_openrouter_usage_details():
    t = _UsageTally()
    t.add({"usage": {
        "prompt_tokens": 1000, "completion_tokens": 200, "cost": "0.005",
        "prompt_tokens_details": {"cached_tokens": 600},
        "completion_tokens_details": {"reasoning_tokens": 150},
    }})
    t.add({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}})
    assert (t.input_tokens, t.output_tokens) == (1010, 205)
    assert (t.cached_tokens, t.reasoning_tokens) == (600, 150)
    assert t.cost_usd == pytest.approx(0.006)
    assert t.live_calls == 2 and t.calls_without_cost == 0
    assert t.cost_source == "openrouter"


def test_tally_tolerates_missing_usage_and_warns(caplog):
    t = _UsageTally()
    t.add({"choices": []})  # no usage block at all
    t.add({"usage": {"prompt_tokens": 3, "completion_tokens": 1, "prompt_tokens_details": None}})
    assert (t.input_tokens, t.output_tokens, t.cached_tokens, t.reasoning_tokens) == (3, 1, 0, 0)
    assert t.cost_usd == 0.0 and t.calls_without_cost == 2
    assert t.cost_source == ""  # never claim a metered figure we didn't get
    with caplog.at_level(logging.WARNING, logger="app.ai.router"):
        t.warn_if_cost_missing("p", "m")
    assert "carried no usage.cost in 2/2" in caplog.text

    ok = _UsageTally()
    ok.add({"usage": {"cost": 0.0}})  # an explicit zero is a real (free) price
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.ai.router"):
        ok.warn_if_cost_missing("p", "m")
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_structured_retry_accumulates_cost(fresh_db, fake_client):
    fake_client.push_json({"control_codevote": ": "})  # garbled → one retry
    fake_client.push_json(VALID)
    with SessionLocal() as db:
        await call_structured(
            db, purpose="t", profile="fast", messages=MESSAGES, schema=_Out, client=fake_client
        )
        mc = db.query(ModelCall).order_by(ModelCall.id.desc()).first()
    assert len(fake_client.calls) == 2
    assert mc.cost_usd == pytest.approx(2 * 0.0123)
    assert mc.cost_source == "openrouter"
    assert (mc.input_tokens, mc.output_tokens) == (200, 100)
    assert (mc.cached_tokens, mc.reasoning_tokens) == (80, 20)


@pytest.mark.asyncio
async def test_text_call_records_cost(fresh_db, fake_client):
    fake_client.push("narrative")
    with SessionLocal() as db:
        await call_text(db, purpose="t", profile="fast", messages=MESSAGES, client=fake_client)
        mc = db.query(ModelCall).order_by(ModelCall.id.desc()).first()
    assert mc.cost_usd == pytest.approx(0.0123)
    assert (mc.cached_tokens, mc.reasoning_tokens) == (40, 10)


def test_additive_migration_adds_usage_columns():
    """An app DB created before cached_tokens/reasoning_tokens gains them at init."""
    from app.config import settings
    from app.db import get_engine, init_db

    get_engine().dispose()
    if settings.db_path.exists():
        settings.db_path.unlink()
    conn = sqlite3.connect(settings.db_path)
    conn.execute(
        "CREATE TABLE model_call (id INTEGER PRIMARY KEY, assessment_id INTEGER, "
        "purpose VARCHAR(60), profile VARCHAR(20), model_id VARCHAR(120), "
        "prompt_sha VARCHAR(64), latency_ms INTEGER, input_tokens INTEGER, "
        "output_tokens INTEGER, cost_usd FLOAT, ok BOOLEAN, error TEXT, "
        "cached BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME)"
    )
    conn.execute(
        "INSERT INTO model_call (purpose, profile, model_id, prompt_sha, latency_ms, "
        "input_tokens, output_tokens, cost_usd, ok, error) "
        "VALUES ('old', 'fast', 'm', 'sha', 1, 2, 3, 0.0, 1, '')"
    )
    conn.commit()
    conn.close()
    try:
        init_db()
        with SessionLocal() as db:
            cols = {
                r[1] for r in db.connection().exec_driver_sql("PRAGMA table_info(model_call)")
            }
            assert {"cached_tokens", "reasoning_tokens", "cost_source"} <= cols
            old = db.query(ModelCall).one()
            assert (old.cached_tokens, old.reasoning_tokens, old.cost_usd) == (0, 0, 0.0)
            assert old.cost_source == ""
    finally:
        get_engine().dispose()


def test_backfill_estimate_prices_only_listed_models():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from backfill_cost import Price, estimate

    pricing = {"m/listed": Price(prompt=1e-6, completion=4e-6)}
    rows = [(1, "m/listed", 1000, 500), (2, "m/listed", 0, 250), (3, "m/gone", 999, 999)]
    costs, summary = estimate(rows, pricing)
    assert costs == {1: pytest.approx(0.003), 2: pytest.approx(0.001)}
    assert summary["m/listed"]["rows"] == 2 and summary["m/listed"]["usd"] == pytest.approx(0.004)
    assert summary["m/gone"]["priced"] is False and 3 not in costs
