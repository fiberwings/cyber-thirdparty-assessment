"""Truncation handling in the OpenRouter client wrappers.

A `finish_reason == "length"` response must never be parsed or persisted:
both wrappers retry with a doubled output budget (up to
`settings.llm_truncation_retries` times, capped at `settings.llm_truncation_cap`),
then fail loudly with `truncated=True`.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.ai.router import OpenRouterError, call_structured, call_text
from app.config import settings
from app.db import SessionLocal


class _Out(BaseModel):
    answer: str


MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_structured_truncation_retries_with_doubled_budget(fresh_db, fake_client):
    fake_client.push_truncated('{"answer": "cut of')
    fake_client.push_json({"answer": "complete"})
    with SessionLocal() as db:
        out = await call_structured(
            db,
            purpose="test",
            profile="fast",
            messages=MESSAGES,
            schema=_Out,
            max_tokens=2048,
            client=fake_client,
        )
    assert out.answer == "complete"
    assert [c["max_tokens"] for c in fake_client.calls] == [2048, 4096]


@pytest.mark.asyncio
async def test_structured_truncation_ladder_takes_two_doublings(fresh_db, fake_client):
    fake_client.push_truncated('{"answer": "cut')
    fake_client.push_truncated('{"answer": "cut again')
    fake_client.push_json({"answer": "complete"})
    with SessionLocal() as db:
        out = await call_structured(
            db,
            purpose="test",
            profile="fast",
            messages=MESSAGES,
            schema=_Out,
            max_tokens=2048,
            client=fake_client,
        )
    assert out.answer == "complete"
    assert [c["max_tokens"] for c in fake_client.calls] == [2048, 4096, 8192]


@pytest.mark.asyncio
async def test_structured_truncation_exhausts_retries_fails_loudly(fresh_db, fake_client):
    for _ in range(settings.llm_truncation_retries + 1):
        fake_client.push_truncated("{}")
    with SessionLocal() as db:
        with pytest.raises(OpenRouterError) as exc:
            await call_structured(
                db,
                purpose="test",
                profile="fast",
                messages=MESSAGES,
                schema=_Out,
                max_tokens=2048,
                client=fake_client,
            )
    assert exc.value.truncated is True
    assert len(fake_client.calls) == settings.llm_truncation_retries + 1


@pytest.mark.asyncio
async def test_structured_budget_capped(fresh_db, fake_client):
    fake_client.push_truncated("{}")
    fake_client.push_truncated("{}")
    fake_client.push_json({"answer": "ok"})
    with SessionLocal() as db:
        out = await call_structured(
            db,
            purpose="test",
            profile="fast",
            messages=MESSAGES,
            schema=_Out,
            max_tokens=12000,
            client=fake_client,
        )
    assert out.answer == "ok"
    assert [c["max_tokens"] for c in fake_client.calls] == [
        12000,
        24000,
        settings.llm_truncation_cap,
    ]


@pytest.mark.asyncio
async def test_structured_default_budget_from_settings(fresh_db, fake_client):
    fake_client.push_json({"answer": "ok"})
    with SessionLocal() as db:
        await call_structured(
            db,
            purpose="test",
            profile="fast",
            messages=MESSAGES,
            schema=_Out,
            client=fake_client,
        )
    assert fake_client.calls[0]["max_tokens"] == settings.llm_budget_small


@pytest.mark.asyncio
async def test_truncation_policy_is_tunable(fresh_db, fake_client, monkeypatch):
    monkeypatch.setattr(settings, "llm_truncation_retries", 1)
    monkeypatch.setattr(settings, "llm_truncation_cap", 4096)
    fake_client.push_truncated("{}")
    fake_client.push_truncated("{}")
    with SessionLocal() as db:
        with pytest.raises(OpenRouterError) as exc:
            await call_structured(
                db,
                purpose="test",
                profile="fast",
                messages=MESSAGES,
                schema=_Out,
                max_tokens=2048,
                client=fake_client,
            )
    assert exc.value.truncated is True
    assert [c["max_tokens"] for c in fake_client.calls] == [2048, 4096]


@pytest.mark.asyncio
async def test_text_truncation_retries_then_fails_loudly(fresh_db, fake_client):
    # First: retry succeeds.
    fake_client.push_truncated("half a narr")
    fake_client.push("full narrative.")
    with SessionLocal() as db:
        text = await call_text(
            db,
            purpose="narrative",
            profile="fast",
            messages=MESSAGES,
            max_tokens=400,
            client=fake_client,
        )
    assert text == "full narrative."
    assert [c["max_tokens"] for c in fake_client.calls] == [400, 800]

    # Second: truncated on every ladder step → loud failure, nothing returned.
    for _ in range(settings.llm_truncation_retries + 1):
        fake_client.push_truncated("half")
    with SessionLocal() as db:
        with pytest.raises(OpenRouterError) as exc:
            await call_text(
                db,
                purpose="narrative",
                profile="fast",
                messages=MESSAGES,
                max_tokens=400,
                client=fake_client,
            )
    assert exc.value.truncated is True
