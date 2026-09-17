"""Azure AI Foundry through the shared streaming engine: request shape, stream
reassembly, the content filter (mid-stream and prompt-level), Retry-After,
the no-keepalive read timeout + heartbeat, unmetered cost, `temp=fixed`, and
mixed OpenRouter/Azure profiles.

Wire shapes follow the Azure OpenAI streaming responses: a leading chunk with
`choices: []` and `prompt_filter_results`, per-choice `content_filter_results`,
and a trailing `{"choices": [], "usage": …}` when `stream_options.include_usage`
is requested. No keepalive comments are ever sent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import httpx
import pytest
import respx
from pydantic import BaseModel

from app import activity
from app.ai.router import LLMClient, LLMError, call_structured, call_text
from app.config import settings
from app.db import SessionLocal
from app.models import LlmCacheEntry, ModelCall
from app.tasks import registry

from .test_router_stream import MESSAGES, USAGE, chunk, sse, stream_response

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
AZ_BASE = "https://acme-eu.openai.azure.com"
AZ_V1_URL = f"{AZ_BASE}/openai/v1/chat/completions"
FD_BASE = "https://acme-eu.services.ai.azure.com"
FD_URL = f"{FD_BASE}/models/chat/completions"
AZ_USAGE = {  # Azure reports the same token details as OpenRouter, but no cost
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "prompt_tokens_details": {"cached_tokens": 40},
    "completion_tokens_details": {"reasoning_tokens": 10},
}


class _Out(BaseModel):
    answer: str


@pytest.fixture()
def azure_env(monkeypatch):
    monkeypatch.setattr(settings, "azure_openai_endpoint", AZ_BASE)
    monkeypatch.setattr(settings, "azure_openai_api_key", "ak")
    monkeypatch.setattr(settings, "azure_openai_api_version", "")
    monkeypatch.setattr(settings, "azure_inference_endpoint", FD_BASE)
    monkeypatch.setattr(settings, "azure_inference_api_key", "fk")
    monkeypatch.setattr(settings, "azure_deployment_meta", "gpt5-prod=openai/gpt-5;temp=fixed,haiku-eu=anthropic/claude-haiku-4.5")


def az_chunk(content: str | None = None, finish: str | None = None, *, filtered: dict | None = None) -> dict:
    """One Azure OpenAI content chunk, annotated with content_filter_results."""
    results = {
        "hate": {"filtered": False, "severity": "safe"},
        "self_harm": {"filtered": False, "severity": "safe"},
        "sexual": {"filtered": False, "severity": "safe"},
        "violence": {"filtered": False, "severity": "safe"},
    }
    if filtered:
        results.update(filtered)
    return {
        "id": "chatcmpl-az1",
        "model": "gpt-5-2025-08-07",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content is not None else {},
                "finish_reason": finish,
                "content_filter_results": results,
            }
        ],
    }


PROMPT_FILTER_CHUNK = {
    "id": "",
    "model": "",
    "object": "",
    "choices": [],
    "prompt_filter_results": [{"prompt_index": 0, "content_filter_results": {"jailbreak": {"filtered": False, "detected": False}}}],
}
ROLE_CHUNK = {"id": "chatcmpl-az1", "model": "gpt-5-2025-08-07", "object": "chat.completion.chunk",
              "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}


def az_stream(*parts: dict, usage: dict | None = AZ_USAGE) -> bytes:
    tail = [] if usage is None else [{"id": "chatcmpl-az1", "model": "gpt-5-2025-08-07",
                                       "object": "chat.completion.chunk", "choices": [], "usage": usage}]
    return sse(PROMPT_FILTER_CHUNK, ROLE_CHUNK, *parts, *tail)


def ok_stream(text: str = '{"answer": "yes"}') -> bytes:
    return az_stream(az_chunk(text), az_chunk(finish="stop"))


# ---------------------------------------------------------------- request shape


@respx.mock
@pytest.mark.asyncio
async def test_azure_openai_request_shape_v1(azure_env):
    route = respx.post(AZ_V1_URL).mock(return_value=stream_response(ok_stream("abc")))
    resp = await LLMClient().chat(MESSAGES, "azure:haiku-eu", max_tokens=100, temperature=0.2,
                                  response_format={"type": "json_object"})
    req = route.calls[0].request
    assert req.headers["api-key"] == "ak" and "authorization" not in req.headers
    assert "http-referer" not in req.headers and "extra-parameters" not in req.headers
    body = json.loads(req.content)
    assert body["model"] == "haiku-eu"
    assert body["max_completion_tokens"] == 100 and "max_tokens" not in body
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["temperature"] == 0.2 and body["response_format"] == {"type": "json_object"}
    assert "provider" not in body
    assert resp["choices"][0]["message"]["content"] == "abc"
    assert resp["_meta"]["dialect"] == "azure-openai" and resp["_meta"]["temperature_sent"] == 0.2


@respx.mock
@pytest.mark.asyncio
async def test_azure_openai_legacy_url_when_api_version_set(azure_env, monkeypatch):
    monkeypatch.setattr(settings, "azure_openai_api_version", "2024-10-21")
    url = f"{AZ_BASE}/openai/deployments/haiku-eu/chat/completions"
    route = respx.post(url, params={"api-version": "2024-10-21"}).mock(return_value=stream_response(ok_stream("x")))
    await LLMClient().chat(MESSAGES, "azure:haiku-eu")
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_foundry_request_shape(azure_env):
    route = respx.post(FD_URL, params={"api-version": "2024-05-01-preview"}).mock(
        return_value=stream_response(ok_stream("abc")))
    resp = await LLMClient().chat(MESSAGES, "foundry:deepseek-v4", max_tokens=100)
    req = route.calls[0].request
    assert req.headers["api-key"] == "fk" and req.headers["extra-parameters"] == "pass-through"
    body = json.loads(req.content)
    assert body["model"] == "deepseek-v4"
    assert body["max_tokens"] == 100 and "max_completion_tokens" not in body
    assert body["stream_options"] == {"include_usage": True}
    assert resp["provider"] == "azure-foundry:acme-eu" and resp["_meta"]["dialect"] == "azure-foundry"


# ---------------------------------------------------------------- reassembly


@respx.mock
@pytest.mark.asyncio
async def test_azure_stream_reassembly(azure_env):
    respx.post(AZ_V1_URL).mock(return_value=stream_response(
        az_stream(az_chunk("a"), az_chunk("b"), az_chunk("c", finish="stop"))))
    resp = await LLMClient().chat(MESSAGES, "azure:haiku-eu")
    choice = resp["choices"][0]
    assert choice["message"]["content"] == "abc"
    assert choice["finish_reason"] == "stop" and choice["native_finish_reason"] == "stop"
    assert resp["provider"] == "azure-openai:acme-eu"
    assert resp["id"] == "chatcmpl-az1" and resp["model"] == "gpt-5-2025-08-07"
    assert resp["usage"] == AZ_USAGE
    assert resp["_meta"]["first_token_ms"] is not None


# ---------------------------------------------------------------- content filter


@respx.mock
@pytest.mark.asyncio
async def test_midstream_content_filter_is_filtered_never_parsed_or_cached(fresh_db, azure_env, monkeypatch):
    """Azure cuts a completion with finish_reason=content_filter and the
    category in content_filter_results. Even though the cut lands on valid
    JSON, the call retries once with fresh context and then fails filtered=True."""
    monkeypatch.setattr(settings, "llm_dev_cache", True)
    cut = az_stream(
        az_chunk('{"answer": "short"}'),
        az_chunk(finish="content_filter", filtered={"violence": {"filtered": True, "severity": "medium"}}),
    )
    route = respx.post(AZ_V1_URL).mock(return_value=stream_response(cut))
    with SessionLocal() as db:
        with pytest.raises(LLMError) as exc:
            await call_structured(db, purpose="t", profile="fast", messages=MESSAGES, schema=_Out,
                                  model_override="azure:haiku-eu")
        assert exc.value.filtered is True
        assert "Azure AI Foundry" in str(exc.value) and "OPENROUTER_PROVIDER_IGNORE" not in str(exc.value)
        assert route.call_count == 2
        assert db.query(LlmCacheEntry).count() == 0
        row = db.query(ModelCall).one()
        assert row.ok is False and row.provider == "azure-openai:acme-eu"
        attempts = row.attempts_json
        assert [a["outcome"] for a in attempts] == ["filtered", "filtered"]
        assert attempts[0]["native_finish_reason"] == "content_filter:violence/medium"
        assert attempts[0]["dialect"] == "azure-openai" and attempts[0]["layer"] == "initial"
        assert attempts[1]["layer"] == "filter"


@respx.mock
@pytest.mark.asyncio
async def test_prompt_filter_400_is_filtered_and_not_retried(fresh_db, azure_env):
    body = {"error": {"code": "content_filter", "status": 400,
                      "message": "The response was filtered due to the prompt triggering Azure OpenAI's content management policy.",
                      "innererror": {"code": "ResponsibleAIPolicyViolation",
                                     "content_filter_result": {"violence": {"filtered": True, "severity": "high"},
                                                               "hate": {"filtered": False, "severity": "safe"}}}}}
    route = respx.post(AZ_V1_URL).mock(return_value=httpx.Response(400, json=body))
    with SessionLocal() as db:
        with pytest.raises(LLMError) as exc:
            await call_text(db, purpose="t", profile="fast", messages=MESSAGES, model_override="azure:haiku-eu")
    assert exc.value.filtered is True and exc.value.transient is False and exc.value.upstream_code == 400
    assert "violence" in str(exc.value) and "content filter" in str(exc.value)
    assert route.call_count == 1


# ---------------------------------------------------------------- 429 / Retry-After


@respx.mock
@pytest.mark.asyncio
async def test_429_retry_after_is_honoured_up_to_the_cap(azure_env, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    route = respx.post(AZ_V1_URL).mock(side_effect=[
        httpx.Response(429, text="busy", headers={"Retry-After": "7"}),
        stream_response(ok_stream("ok")),
    ])
    resp = await LLMClient().chat(MESSAGES, "azure:haiku-eu")
    assert resp["choices"][0]["message"]["content"] == "ok" and route.call_count == 2
    assert slept == [7.0]
    assert resp["_meta"]["retries"][0]["retry_after_s"] == 7.0

    slept.clear()
    monkeypatch.setattr(settings, "llm_retry_after_cap_s", 3.0)
    respx.post(AZ_V1_URL).mock(side_effect=[
        httpx.Response(429, text="busy", headers={"retry-after-ms": "9000"}),
        stream_response(ok_stream("ok")),
    ])
    await LLMClient().chat(MESSAGES, "azure:haiku-eu")
    assert slept == [3.0]


@respx.mock
@pytest.mark.asyncio
async def test_429_exhausting_the_retries_raises_transient(azure_env, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(settings, "llm_transient_retries", 3)
    route = respx.post(AZ_V1_URL).mock(return_value=httpx.Response(
        429, text="busy", headers={"x-ratelimit-limit-tokens": "100000", "x-ratelimit-limit-requests": "100"}
    ))
    with pytest.raises(LLMError) as exc:
        await LLMClient().chat(MESSAGES, "azure:haiku-eu")
    assert exc.value.transient is True and exc.value.upstream_code == 429 and route.call_count == 4
    assert slept == [1.5, 3.0, 6.0]
    assert exc.value.rate_limit == {"limit-tokens": "100000", "limit-requests": "100"}
    assert len(exc.value.retries) == 3


# ---------------------------------------------------------------- no keepalives


async def _silent_server(delay: float):
    """A real server that sends headers, stays silent for `delay`, then the answer."""
    hits = {"n": 0}

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        hits["n"] += 1
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.sleep(delay)
            writer.write(ok_stream("late"))
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", hits


@pytest.mark.asyncio
async def test_silent_azure_stream_survives_the_keepalive_idle_tier(azure_env, monkeypatch):
    """A stream silent for longer than LLM_STREAM_IDLE_S (which assumes
    keepalives) but shorter than LLM_STREAM_IDLE_NO_KEEPALIVE_S completes on
    Azure and times out (then retries) on OpenRouter."""
    monkeypatch.setattr(settings, "llm_stream_idle_s", 0.1)
    monkeypatch.setattr(settings, "llm_stream_idle_no_keepalive_s", 2.0)
    monkeypatch.setattr(settings, "llm_transient_retries", 1)  # one real backoff sleep
    server, base, hits = await _silent_server(delay=0.4)
    monkeypatch.setattr(settings, "azure_openai_endpoint", base)
    try:
        resp = await LLMClient().chat(MESSAGES, "azure:haiku-eu")
        assert resp["choices"][0]["message"]["content"] == "late" and hits["n"] == 1
        with pytest.raises(LLMError) as exc:
            await LLMClient(api_key="k", base_url=f"{base}/api/v1").chat(MESSAGES, "test/model")
        assert "LLM_STREAM_IDLE_S" in str(exc.value) and hits["n"] == 3
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_silent_azure_stream_times_out_at_the_no_keepalive_tier(azure_env, monkeypatch):
    monkeypatch.setattr(settings, "llm_stream_idle_s", 0.05)
    monkeypatch.setattr(settings, "llm_stream_idle_no_keepalive_s", 0.15)
    monkeypatch.setattr(settings, "llm_transient_retries", 1)
    server, base, hits = await _silent_server(delay=5)
    monkeypatch.setattr(settings, "azure_openai_endpoint", base)
    try:
        with pytest.raises(LLMError) as exc:
            await LLMClient().chat(MESSAGES, "azure:haiku-eu")
        assert exc.value.transient is True and "azure-openai" in str(exc.value)
        assert hits["n"] == 2  # retried once before any output
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_heartbeat_keeps_the_ambient_task_alive_during_silence(fresh_db, azure_env, monkeypatch):
    monkeypatch.setattr(settings, "llm_stream_idle_no_keepalive_s", 2.0)
    monkeypatch.setattr(settings, "task_watchdog_interval_s", 0.02)
    server, base, _ = await _silent_server(delay=0.3)
    monkeypatch.setattr(settings, "azure_openai_endpoint", base)
    touches: list[float] = []

    async def job(handle):
        real_touch = handle.touch
        monkeypatch.setattr(handle, "touch", lambda: (touches.append(asyncio.get_event_loop().time()), real_touch()))
        await LLMClient().chat(MESSAGES, "azure:haiku-eu")

    try:
        task = registry.submit(job, kind="t")
        await asyncio.wait_for(task._task, 5)
    finally:
        server.close()
        await server.wait_closed()
    # Many pings landed during the 0.3 s of silence, not just on the SSE lines.
    silent_pings = [t for t in touches if t < touches[0] + 0.25]
    assert len(silent_pings) >= 5
    assert activity.current_task.get() is None


# ---------------------------------------------------------------- cost (unmetered)


@respx.mock
@pytest.mark.asyncio
async def test_azure_calls_record_unmetered_cost_without_warning(fresh_db, azure_env, caplog):
    respx.post(AZ_V1_URL).mock(return_value=stream_response(ok_stream()))
    with SessionLocal() as db, caplog.at_level(logging.WARNING):
        out = await call_structured(db, purpose="t", profile="fast", messages=MESSAGES, schema=_Out,
                                    model_override="azure:haiku-eu")
        assert out.answer == "yes"
        row = db.query(ModelCall).one()
    assert row.ok is True and row.cost_usd == 0 and row.cost_source == ""
    assert row.input_tokens == 100 and row.output_tokens == 50
    assert row.cached_tokens == 40 and row.reasoning_tokens == 10
    assert row.provider == "azure-openai:acme-eu"
    assert not [r for r in caplog.records if "under-reported" in r.message]


@respx.mock
@pytest.mark.asyncio
async def test_openrouter_call_without_cost_still_warns(fresh_db, caplog):
    respx.post(OR_URL).mock(return_value=stream_response(
        sse(chunk('{"answer": "yes"}', finish="stop"), chunk(finish="stop", usage=AZ_USAGE))))
    with SessionLocal() as db, caplog.at_level(logging.WARNING):
        await call_structured(db, purpose="t", profile="fast", messages=MESSAGES, schema=_Out,
                              model_override="test/model")
        row = db.query(ModelCall).one()
    assert row.cost_source == "" and row.cost_usd == 0
    assert [r for r in caplog.records if "under-reported" in r.message]


# ---------------------------------------------------------------- temperature


@respx.mock
@pytest.mark.asyncio
async def test_temp_fixed_omits_temperature_and_records_it(fresh_db, azure_env):
    route = respx.post(AZ_V1_URL).mock(return_value=stream_response(ok_stream()))
    with SessionLocal() as db:
        await call_structured(db, purpose="t", profile="reasoner", messages=MESSAGES, schema=_Out,
                              model_override="azure:gpt5-prod")
        row = db.query(ModelCall).one()
    body = json.loads(route.calls[0].request.content)
    assert "temperature" not in body and body["model"] == "gpt5-prod"
    assert row.attempts_json[0]["temperature_sent"] is None
    assert row.attempts_json[0]["dialect"] == "azure-openai"


@respx.mock
@pytest.mark.asyncio
async def test_unflagged_reasoning_deployment_400_surfaces_the_meta_hint(azure_env):
    body = {"error": {"code": "unsupported_value", "param": "temperature",
                      "message": "Unsupported value: 'temperature' does not support 0.2 with this model. Only the default (1) value is supported."}}
    route = respx.post(AZ_V1_URL).mock(return_value=httpx.Response(400, json=body))
    with pytest.raises(LLMError) as exc:
        await LLMClient().chat(MESSAGES, "azure:haiku-eu", temperature=0.2)
    assert exc.value.transient is False and route.call_count == 1
    assert "temp=fixed" in str(exc.value) and "AZURE_DEPLOYMENT_META" in str(exc.value)


# ---------------------------------------------------------------- mixed profiles / credentials


@respx.mock
@pytest.mark.asyncio
async def test_mixed_profiles_hit_their_own_hosts(fresh_db, azure_env, monkeypatch):
    monkeypatch.setattr(settings, "model_fast", "test/model")
    monkeypatch.setattr(settings, "model_reasoner", "azure:gpt5-prod")
    orr = respx.post(OR_URL).mock(return_value=stream_response(
        sse(chunk('{"answer": "fast"}', finish="stop"), chunk(finish="stop", usage=USAGE))))
    azr = respx.post(AZ_V1_URL).mock(return_value=stream_response(ok_stream('{"answer": "deep"}')))
    with SessionLocal() as db:
        fast = await call_structured(db, purpose="a", profile="fast", messages=MESSAGES, schema=_Out)
        deep = await call_structured(db, purpose="b", profile="reasoner", messages=MESSAGES, schema=_Out)
        rows = {r.purpose: r for r in db.query(ModelCall).all()}
    assert (fast.answer, deep.answer) == ("fast", "deep")
    assert orr.call_count == 1 and azr.call_count == 1
    assert rows["a"].cost_source == "openrouter" and rows["a"].cost_usd == pytest.approx(0.0123)
    assert rows["b"].cost_source == "" and rows["b"].provider == "azure-openai:acme-eu"
    assert rows["b"].model_id == "azure:gpt5-prod"


@pytest.mark.asyncio
async def test_missing_azure_credentials_fail_before_any_request(monkeypatch):
    monkeypatch.setattr(settings, "azure_openai_endpoint", "")
    monkeypatch.setattr(settings, "azure_openai_api_key", "")
    with pytest.raises(LLMError) as exc:
        await LLMClient().chat(MESSAGES, "azure:x")
    assert "AZURE_OPENAI_ENDPOINT" in str(exc.value)


@pytest.mark.asyncio
async def test_unknown_scheme_is_a_loud_error():
    with pytest.raises(ValueError):
        await LLMClient().chat(MESSAGES, "bogus:x")


def test_patch_model_overrides_rejects_azure_ref_without_key(fresh_db, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main as main_mod
    from app.models import Assessment

    monkeypatch.setattr(settings, "azure_openai_endpoint", AZ_BASE)
    monkeypatch.setattr(settings, "azure_openai_api_key", "")
    with SessionLocal() as db:
        a = Assessment(vendor_name="Acme")
        db.add(a)
        db.commit()
        aid = a.id
    with TestClient(main_mod.app) as c:
        resp = c.patch(f"/api/assessments/{aid}/model-overrides", json={"gap_analysis": "azure:gpt5-prod"})
        assert resp.status_code == 422 and "AZURE_OPENAI_API_KEY" in resp.json()["detail"]
        resp = c.patch(f"/api/assessments/{aid}/model-overrides", json={"gap_analysis": "bogus:x"})
        assert resp.status_code == 422 and "scheme" in resp.json()["detail"]
