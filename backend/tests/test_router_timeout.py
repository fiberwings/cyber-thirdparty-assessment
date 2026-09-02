"""HTTP timeout scaling with the requested output budget.

The client is non-streaming, so the per-attempt read timeout must cover the
entire generation: base + tokens / assumed_tps, clamped to the max.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.ai.router import _timeout_for_budget, call_structured
from app.config import settings
from app.db import SessionLocal


class _Out(BaseModel):
    answer: str


MESSAGES = [{"role": "user", "content": "hi"}]


def test_timeout_scales_with_budget(monkeypatch):
    monkeypatch.setattr(settings, "llm_timeout_base_s", 60.0)
    monkeypatch.setattr(settings, "llm_assumed_output_tps", 40.0)
    monkeypatch.setattr(settings, "llm_timeout_max_s", 600.0)
    assert _timeout_for_budget(4096) == pytest.approx(60.0 + 4096 / 40.0)
    assert _timeout_for_budget(16384) == pytest.approx(60.0 + 16384 / 40.0)


def test_timeout_clamped_at_max(monkeypatch):
    monkeypatch.setattr(settings, "llm_timeout_base_s", 60.0)
    monkeypatch.setattr(settings, "llm_assumed_output_tps", 40.0)
    monkeypatch.setattr(settings, "llm_timeout_max_s", 600.0)
    assert _timeout_for_budget(32768) == 600.0


@pytest.mark.asyncio
async def test_timeout_plumbed_through_to_client(fresh_db, fake_client):
    fake_client.push_json({"answer": "ok"})
    with SessionLocal() as db:
        await call_structured(
            db,
            purpose="test",
            profile="fast",
            messages=MESSAGES,
            schema=_Out,
            max_tokens=8192,
            client=fake_client,
        )
    assert fake_client.calls[0]["timeout"] == pytest.approx(_timeout_for_budget(8192))
