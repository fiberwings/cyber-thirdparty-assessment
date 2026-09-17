"""Streaming transport of `LLMClient.chat` and its liveness deadlines.

The client streams SSE and reassembles the familiar non-streaming dict, so
every consumer (truncation ladder, dev cache, fake client) is unchanged.
Deadlines are inactivity-based: a dead socket (LLM_STREAM_IDLE_S), a live
socket with no output token (LLM_CONTENT_SILENCE_S), and a hard ceiling
(LLM_CALL_MAX_S). Automatic retry (bounded, exponential backoff, Retry-After
honoured) only before any output token; never after partial output.
Cancellation closes the stream.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx
import pytest
import respx
from pydantic import BaseModel

from app.ai.router import LLMClient, LLMError, call_structured
from app.config import settings
from app.db import SessionLocal
from app.models import LlmCacheEntry, ModelCall

BASE = "https://openrouter.ai/api/v1"
URL = f"{BASE}/chat/completions"
USAGE = {
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "cost": 0.0123,
    "prompt_tokens_details": {"cached_tokens": 40},
    "completion_tokens_details": {"reasoning_tokens": 10},
}
MESSAGES = [{"role": "user", "content": "hi"}]


class _Out(BaseModel):
    answer: str


def chunk(content: str | None = None, finish: str | None = None, usage: dict | None = None, **extra) -> dict:
    c = {
        "id": "gen-1",
        "model": "test/model",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": content or ""},
                "finish_reason": finish,
                "native_finish_reason": finish,
            }
        ],
    }
    if usage is not None:
        c["usage"] = usage
    c.update(extra)
    return c


def sse(*events, done: bool = True) -> bytes:
    """Raw strings are emitted as-is (comments / other SSE fields); dicts as data events."""
    out = []
    for e in events:
        out.append(e + "\n\n" if isinstance(e, str) else "data: " + json.dumps(e) + "\n\n")
    if done:
        out.append("data: [DONE]\n\n")
    return "".join(out).encode()


def stream_response(body: bytes | AsyncIterator[bytes], status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/event-stream"}, content=body)


def client() -> LLMClient:
    return LLMClient(api_key="k", base_url=BASE)


# ---------------------------------------------------------------- reassembly


@respx.mock
@pytest.mark.asyncio
async def test_reassembles_content_skips_keepalives_and_captures_usage():
    route = respx.post(URL).mock(
        return_value=stream_response(
            sse(
                ": OPENROUTER PROCESSING",
                chunk("a"),
                "event: ping",
                ": OPENROUTER PROCESSING",
                chunk("b"),
                chunk("c", finish="stop"),
                chunk(finish="stop", usage=USAGE),
            )
        )
    )
    resp = await client().chat(MESSAGES, "test/model", max_tokens=100)
    assert resp["choices"][0]["message"]["content"] == "abc"
    assert resp["choices"][0]["finish_reason"] == "stop"
    assert resp["usage"] == USAGE
    assert resp["_meta"]["first_token_ms"] is not None
    body = json.loads(route.calls[0].request.content)
    assert body["stream"] is True
    assert "usage" not in body and "stream_options" not in body
    assert body["max_tokens"] == 100


@respx.mock
@pytest.mark.asyncio
async def test_truncated_stream_is_never_parsed_or_cached(fresh_db, monkeypatch):
    """`finish_reason: length` rides on the last content chunk and the usage
    chunk; the ladder is exhausted immediately here → loud truncated error,
    nothing in the dev cache, the partial content kept for forensics."""
    monkeypatch.setattr(settings, "llm_truncation_retries", 0)
    monkeypatch.setattr(settings, "llm_dev_cache", True)
    respx.post(URL).mock(
        return_value=stream_response(
            sse(chunk('{"answer": "cut'), chunk(finish="length"), chunk(finish="length", usage=USAGE))
        )
    )
    with SessionLocal() as db:
        with pytest.raises(LLMError) as exc:
            await call_structured(
                db, purpose="t", profile="fast", messages=MESSAGES, schema=_Out, client=client()
            )
        assert exc.value.truncated is True
        assert db.query(LlmCacheEntry).count() == 0
        row = db.query(ModelCall).one()
        assert row.ok is False and row.output_head == '{"answer": "cut'
        assert row.first_token_ms is not None
        assert row.cost_usd == pytest.approx(0.0123)


@respx.mock
@pytest.mark.asyncio
async def test_dev_cache_stores_answer_without_meta_and_replays(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "llm_dev_cache", True)
    route = respx.post(URL).mock(
        return_value=stream_response(sse(chunk('{"answer": "ok"}', finish="stop"), chunk(finish="stop", usage=USAGE)))
    )
    with SessionLocal() as db:
        for _ in range(2):
            out = await call_structured(
                db, purpose="t", profile="fast", messages=MESSAGES, schema=_Out, client=client()
            )
            assert out.answer == "ok"
        assert route.call_count == 1
        entry = db.query(LlmCacheEntry).one()
        assert "_meta" not in entry.response_json
        rows = db.query(ModelCall).order_by(ModelCall.id).all()
        assert [r.cached for r in rows] == [False, True]
        assert rows[0].ok and rows[0].output_head is None and rows[0].first_token_ms is not None
        assert rows[1].first_token_ms is None


# ---------------------------------------------------------------- errors / retry policy


@pytest.fixture()
def no_wait(monkeypatch):
    """Record the engine's backoff sleeps instead of waiting them out."""
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


@respx.mock
@pytest.mark.asyncio
async def test_midstream_error_after_content_is_partial_and_not_retried():
    err = {"error": {"code": 502, "message": "provider died"}}
    route = respx.post(URL).mock(
        return_value=stream_response(sse(chunk("par"), chunk(finish="error", **err), done=False))
    )
    with pytest.raises(LLMError) as exc:
        await client().chat(MESSAGES, "test/model")
    assert exc.value.partial is True and exc.value.transient is False
    assert exc.value.upstream_code == 502 and exc.value.output_head == "par"
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_midstream_transient_error_before_content_is_retried_until_exhausted(no_wait, monkeypatch):
    monkeypatch.setattr(settings, "llm_transient_retries", 2)
    err = {"error": {"code": 502, "message": "provider died"}}
    route = respx.post(URL).mock(return_value=stream_response(sse(chunk(finish="error", **err), done=False)))
    with pytest.raises(LLMError) as exc:
        await client().chat(MESSAGES, "test/model")
    assert exc.value.transient is True and exc.value.partial is False
    assert route.call_count == 3 and no_wait == [1.5, 3.0]
    # The final error carries the retry history for attempts_json.
    assert [r["n"] for r in exc.value.retries] == [1, 2]
    assert exc.value.retries[0]["upstream_code"] == 502 and exc.value.retries[0]["wait_s"] == 1.5


@respx.mock
@pytest.mark.asyncio
async def test_finish_reason_error_without_envelope_is_retried_before_content(no_wait):
    route = respx.post(URL).mock(side_effect=[
        stream_response(sse(chunk(finish="error"), done=False)),
        stream_response(sse(chunk("ok", finish="stop"), chunk(finish="stop", usage=USAGE))),
    ])
    resp = await client().chat(MESSAGES, "test/model")
    assert resp["choices"][0]["message"]["content"] == "ok" and route.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_http_429_before_stream_is_retried_with_backoff_then_raised(no_wait, monkeypatch, caplog):
    """Every transient status gets the same bounded loop: 1.5 s doubling,
    capped by LLM_RETRY_AFTER_CAP_S, LLM_TRANSIENT_RETRIES times. A 429
    also carries the provider's rate-limit headers into the error, the
    retry record and the WARNING, next to the budget this call reserved."""
    monkeypatch.setattr(settings, "llm_transient_retries", 6)
    monkeypatch.setattr(settings, "llm_retry_after_cap_s", 20.0)
    route = respx.post(URL).mock(return_value=httpx.Response(
        429, text="slow down",
        headers={"x-ratelimit-limit-tokens": "100000", "x-ratelimit-remaining-tokens": "0"},
    ))
    with caplog.at_level("WARNING", logger="app.ai.router"):
        with pytest.raises(LLMError) as exc:
            await client().chat(MESSAGES, "test/model", max_tokens=65536)
    assert exc.value.upstream_code == 429 and exc.value.transient is True
    assert route.call_count == 7
    assert no_wait == [1.5, 3.0, 6.0, 12.0, 20.0, 20.0]
    assert exc.value.rate_limit == {"limit-tokens": "100000", "remaining-tokens": "0"}
    assert len(exc.value.retries) == 6 and exc.value.retries[0]["rate_limit"]["limit-tokens"] == "100000"
    retry_logs = [r.getMessage() for r in caplog.records if "retry 1/6" in r.getMessage()]
    assert len(retry_logs) == 1
    assert "limit-tokens=100000" in retry_logs[0] and "max_tokens=65536" in retry_logs[0]


@respx.mock
@pytest.mark.asyncio
async def test_transient_retry_recovers_and_records_history(no_wait):
    """A 503 then success: Retry-After is honoured on 503 too (it stretches
    the backoff, never shortens it) and the recovered response carries the
    retry history in `_meta` so the attempt record shows what it survived."""
    route = respx.post(URL).mock(side_effect=[
        httpx.Response(503, text="upstream busy", headers={"Retry-After": "4"}),
        httpx.Response(502, text="bad gateway", headers={"Retry-After": "1"}),
        stream_response(sse(chunk("ok", finish="stop"), chunk(finish="stop", usage=USAGE))),
    ])
    resp = await client().chat(MESSAGES, "test/model")
    assert resp["choices"][0]["message"]["content"] == "ok" and route.call_count == 3
    assert no_wait == [4.0, 3.0]  # 503: max(1.5, 4); 502: backoff 3.0 (Retry-After not read on 502)
    assert [r["upstream_code"] for r in resp["_meta"]["retries"]] == [503, 502]
    assert resp["_meta"]["retries"][0]["retry_after_s"] == 4.0


@respx.mock
@pytest.mark.asyncio
async def test_transient_retries_zero_disables_the_loop(no_wait, monkeypatch):
    monkeypatch.setattr(settings, "llm_transient_retries", 0)
    route = respx.post(URL).mock(return_value=httpx.Response(503, text="busy"))
    with pytest.raises(LLMError) as exc:
        await client().chat(MESSAGES, "test/model")
    assert route.call_count == 1 and no_wait == [] and exc.value.retries == []


@respx.mock
@pytest.mark.asyncio
async def test_transport_retries_land_on_attempts_json(fresh_db, no_wait):
    """The engine's retries are per-attempt forensics: a structured call
    that survived a 429 records it on model_call.attempts_json, and one
    that did not shows every wait it made."""
    respx.post(URL).mock(side_effect=[
        httpx.Response(429, text="slow down", headers={"x-ratelimit-limit-tokens": "100000"}),
        stream_response(sse(chunk('{"answer": "ok"}', finish="stop"), chunk(finish="stop", usage=USAGE))),
    ])
    with SessionLocal() as db:
        out = await call_structured(
            db, purpose="t", profile="fast", messages=MESSAGES, schema=_Out, client=client()
        )
        assert out.answer == "ok"
        row = db.query(ModelCall).one()
        attempts = row.attempts_json
        assert row.ok is True and attempts[0]["outcome"] == "ok"
        assert [r["upstream_code"] for r in attempts[0]["transport_retries"]] == [429]
        assert attempts[0]["transport_retries"][0]["rate_limit"] == {"limit-tokens": "100000"}


@respx.mock
@pytest.mark.asyncio
async def test_http_400_is_not_retried():
    route = respx.post(URL).mock(return_value=httpx.Response(400, text="bad request"))
    with pytest.raises(LLMError) as exc:
        await client().chat(MESSAGES, "test/model")
    assert exc.value.transient is False and route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_empty_stream_is_transient(no_wait, monkeypatch):
    monkeypatch.setattr(settings, "llm_transient_retries", 1)
    respx.post(URL).mock(return_value=stream_response(sse()))
    with pytest.raises(LLMError) as exc:
        await client().chat(MESSAGES, "test/model")
    assert exc.value.transient is True and "empty stream" in str(exc.value)


# ---------------------------------------------------------------- liveness tiers


async def _keepalives_forever(period: float = 0.01) -> AsyncIterator[bytes]:
    while True:
        yield b": OPENROUTER PROCESSING\n\n"
        await asyncio.sleep(period)


@respx.mock
@pytest.mark.asyncio
async def test_content_silence_raises_and_is_not_retried(monkeypatch):
    """Tier (ii): the socket is alive (keepalives flow) but no output token
    arrives for LLM_CONTENT_SILENCE_S — a stuck generation, reported loudly
    and never retried (a retry would just wait again)."""
    monkeypatch.setattr(settings, "llm_content_silence_s", 0.05)
    route = respx.post(URL).mock(side_effect=lambda req: stream_response(_keepalives_forever()))
    with pytest.raises(LLMError) as exc:
        await client().chat(MESSAGES, "test/model")
    assert "LLM_CONTENT_SILENCE_S" in str(exc.value)
    assert exc.value.transient is False and exc.value.partial is False
    assert route.call_count == 1


async def _reasoning_then_answer(period: float = 0.01) -> AsyncIterator[bytes]:
    for _ in range(12):
        yield sse({"choices": [{"index": 0, "delta": {"reasoning": "hmm"}, "finish_reason": None}]}, done=False)
        await asyncio.sleep(period)
    yield sse(chunk("42", finish="stop"), chunk(finish="stop", usage=USAGE))


@respx.mock
@pytest.mark.asyncio
async def test_streamed_reasoning_counts_as_activity_for_the_silence_tier(monkeypatch):
    """A thinking model that streams reasoning deltas is alive: with a silence
    limit shorter than the reasoning phase the call still completes, and the
    reasoning never leaks into the content."""
    monkeypatch.setattr(settings, "llm_content_silence_s", 0.05)
    respx.post(URL).mock(side_effect=lambda req: stream_response(_reasoning_then_answer()))
    resp = await client().chat(MESSAGES, "test/model")
    assert resp["choices"][0]["message"]["content"] == "42"


@respx.mock
@pytest.mark.asyncio
async def test_hard_ceiling_raises_non_transient(monkeypatch):
    monkeypatch.setattr(settings, "llm_content_silence_s", 10.0)
    monkeypatch.setattr(settings, "llm_call_max_s", 0.1)
    route = respx.post(URL).mock(side_effect=lambda req: stream_response(_keepalives_forever()))
    with pytest.raises(LLMError) as exc:
        await client().chat(MESSAGES, "test/model")
    assert "LLM_CALL_MAX_S" in str(exc.value)
    assert exc.value.transient is False and route.call_count == 1


@pytest.mark.asyncio
async def test_dead_socket_before_output_times_out_and_retries(monkeypatch):
    """Tier (i): a real local server that sends headers and then nothing at
    all. httpx's read timeout (LLM_STREAM_IDLE_S) fires; before any output
    token that is transient, so the call is attempted again (retries
    bounded to one here; the backoff sleep is real — the handler needs
    asyncio.sleep)."""
    monkeypatch.setattr(settings, "llm_stream_idle_s", 0.1)
    monkeypatch.setattr(settings, "llm_transient_retries", 1)
    hits = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        nonlocal hits
        hits += 1
        while True:  # consume request headers + body
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.sleep(5)
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        cli = LLMClient(api_key="k", base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(LLMError) as exc:
            await cli.chat(MESSAGES, "test/model")
        assert exc.value.transient is True and exc.value.partial is False
        assert "before any output" in str(exc.value)
        assert hits == 2
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------- cancellation


class _RecordingStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield sse(chunk("partial"), done=False)
        await asyncio.sleep(10)

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
@pytest.mark.asyncio
async def test_cancelling_the_caller_closes_the_stream():
    stream = _RecordingStream()
    respx.post(URL).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    )
    task = asyncio.create_task(client().chat(MESSAGES, "test/model"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed is True


@respx.mock
@pytest.mark.asyncio
async def test_cancelled_structured_call_records_model_call(fresh_db):
    stream = _RecordingStream()
    respx.post(URL).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    )
    with SessionLocal() as db:
        task = asyncio.create_task(
            call_structured(db, purpose="t", profile="fast", messages=MESSAGES, schema=_Out, client=client())
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = db.query(ModelCall).one()
        assert row.ok is False and row.error == "cancelled"


@respx.mock
@pytest.mark.asyncio
async def test_provider_ignore_list_is_sent_when_configured(monkeypatch):
    from app.config import settings

    route = respx.post(f"{BASE}/chat/completions").mock(
        return_value=stream_response(sse(chunk("ok", finish="stop", usage=USAGE)))
    )
    monkeypatch.setattr(settings, "openrouter_provider_ignore", "StreamLake, Other")
    await client().chat(MESSAGES, "test/model", max_tokens=100)
    body = json.loads(route.calls[0].request.content)
    assert body["provider"] == {"ignore": ["StreamLake", "Other"]}

    monkeypatch.setattr(settings, "openrouter_provider_ignore", "")
    await client().chat(MESSAGES, "test/model", max_tokens=100)
    assert "provider" not in json.loads(route.calls[1].request.content)
