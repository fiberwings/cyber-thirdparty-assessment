"""Truncation handling in the OpenRouter client wrappers.

A `finish_reason == "length"` response must never be parsed or persisted:
both wrappers retry once with a doubled output budget (capped), then fail
loudly with `truncated=True`.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.ai.router import OpenRouterError, call_structured, call_text
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
async def test_structured_truncation_twice_fails_loudly(fresh_db, fake_client):
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
    assert len(fake_client.calls) == 2


@pytest.mark.asyncio
async def test_structured_budget_capped_at_16384(fresh_db, fake_client):
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
    assert fake_client.calls[1]["max_tokens"] == 16384


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

    # Second: truncated twice → loud failure, nothing returned.
    fake_client.push_truncated("half")
    fake_client.push_truncated("half again")
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
