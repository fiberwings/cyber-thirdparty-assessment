"""OpenRouter client + structured-output helper.

Profiles:
    fast      → cheap/fast model (default Haiku 4.5) for Q&A loop, ingestion,
                document classification.
    reasoner  → heavy reasoner (default Opus 4.7; GPT-5 selectable) for scenario
                generation, gap analysis, weakness synthesis.

`call_structured` does:
    1. POST /chat/completions (streamed) with response_format=json_object
    2. Parse JSON, validate against the supplied Pydantic model
    3. On validation failure, retry ONCE with a stricter follow-up message
    4. Persist a ModelCall row regardless of outcome

Transport: completions are streamed (SSE) and reassembled into the familiar
non-streaming response dict, so every consumer (the retry ladder, the dev
cache, tests) sees one shape. Streaming is what makes deadlines
liveness-based instead of duration-based: the router observes every token
and keepalive, times out only on *inactivity* (two tiers) plus a hard
ceiling sized in hours, pings the ambient task (app.activity) so the
watchdog sees progress, and can be cancelled mid-generation (which stops
billing on providers that support it). Automatic retry happens only before
any content has arrived — never after partial output (no double billing,
no silent fallback).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app import activity
from app.config import settings
from app.models import ModelCall

T = TypeVar("T", bound=BaseModel)


@dataclass
class ModelProfile:
    name: str            # "fast" | "reasoner"
    default_model: str
    alternatives: list[str]


def get_profiles() -> dict[str, ModelProfile]:
    return {
        "fast": ModelProfile(
            name="fast",
            default_model=settings.model_fast,
            alternatives=settings.fast_alternatives,
        ),
        "reasoner": ModelProfile(
            name="reasoner",
            default_model=settings.model_reasoner,
            alternatives=settings.reasoner_alternatives,
        ),
    }


def _resolve_model(profile: str, override: str | None) -> str:
    if override:
        return override
    profiles = get_profiles()
    if profile not in profiles:
        raise ValueError(f"Unknown profile: {profile}")
    return profiles[profile].default_model


# ---------- model capability guard ----------

_logger = logging.getLogger(__name__)

# (context_window, max_output_tokens) for the shipped model catalogue,
# verified against OpenRouter's live model metadata on 2026-09-02.
MODEL_CAPS: dict[str, tuple[int, int]] = {
    "anthropic/claude-opus-4.7": (1_000_000, 128_000),
    "anthropic/claude-sonnet-4.6": (1_000_000, 128_000),
    "anthropic/claude-haiku-4.5": (200_000, 64_000),
    "openai/gpt-5": (400_000, 128_000),
    "openai/gpt-5-mini": (400_000, 128_000),
}

# The whole-bundle gap-analysis path packs up to ~100K input tokens plus
# prompts, so reasoner models need a genuinely large context window.
_REASONER_MIN_CONTEXT = 200_000
_FAST_MIN_CONTEXT = 100_000


def validate_model_capability(model_id: str, profile: str) -> str | None:
    """Return a human-readable rejection reason if `model_id` cannot honour
    the configured output-budget policy for `profile`, else None.

    Unknown model ids get a logged warning, not a rejection: OpenRouter's
    catalogue moves faster than this table, and hard-failing novel models
    would be an availability regression.
    """
    caps = MODEL_CAPS.get(model_id)
    if caps is None:
        _logger.warning(
            "Model '%s' is not in the local capability table; cannot verify it "
            "supports the configured output budgets (up to %d tokens).",
            model_id,
            settings.llm_truncation_cap,
        )
        return None
    ctx, max_out = caps
    if profile == "reasoner":
        need_out = settings.llm_min_model_output_cap
        need_ctx = _REASONER_MIN_CONTEXT
    else:
        # Deepest ladder a fast-profile call can reach: medium-tier start
        # (attestation), doubled llm_truncation_retries times, clamped.
        need_out = min(
            settings.llm_truncation_cap,
            settings.llm_budget_medium * (2 ** settings.llm_truncation_retries),
        )
        need_ctx = _FAST_MIN_CONTEXT
    if max_out < need_out:
        return (
            f"model '{model_id}' supports at most {max_out} output tokens, but the "
            f"{profile} profile's truncation ladder can request up to {need_out} — "
            "truncated (incomplete) assessments would be unavoidable"
        )
    if ctx < need_ctx:
        return (
            f"model '{model_id}' has a {ctx}-token context window; the {profile} "
            f"profile requires at least {need_ctx} to fit the evidence bundle"
        )
    return None


def warn_if_configured_models_undersized() -> None:
    """Startup check: the configured profile defaults must honour the budget
    policy. Logged at ERROR (not raised) so a misconfiguration is loud without
    taking the app down."""
    for profile in ("fast", "reasoner"):
        model_id = get_profiles()[profile].default_model
        reason = validate_model_capability(model_id, profile)
        if reason is not None:
            _logger.error("Configured %s model fails the capability check: %s", profile, reason)


def _hash_prompt(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()


# ---------- dev-only response cache ----------
#
# Keyed by everything that determines the model's answer: model id, the full
# message list, sampling parameters and response format. Only consulted when
# Settings.llm_dev_cache_active (never in production). A hit is recorded on
# ModelCall with cached=True and zero tokens so cost accounting stays honest.


def _cache_key(
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    max_tokens: int,
    response_format: dict[str, Any] | None,
) -> str:
    payload = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": response_format,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _cache_get(db: Session, key: str) -> dict[str, Any] | None:
    from app.models import LlmCacheEntry

    try:
        row = db.get(LlmCacheEntry, key)
    except Exception:
        return None
    return dict(row.response_json) if row is not None else None


def _cache_put(db: Session, key: str, model: str, prompt_sha: str, response: dict[str, Any]) -> None:
    from app.models import LlmCacheEntry

    # Per-call transport telemetry (`_meta`) is not part of the answer.
    stored = {k: v for k, v in response.items() if not str(k).startswith("_")}
    try:
        if db.get(LlmCacheEntry, key) is None:
            db.add(LlmCacheEntry(key=key, model_id=model, prompt_sha=prompt_sha, response_json=stored))
            db.commit()
    except Exception:
        db.rollback()


async def _chat_maybe_cached(
    db: Session,
    cli: "OpenRouterClient",
    messages: list[dict[str, Any]],
    model: str,
    *,
    response_format: dict[str, Any] | None,
    temperature: float,
    max_tokens: int,
    prompt_sha: str,
) -> tuple[dict[str, Any], bool]:
    """cli.chat() behind the dev cache. Returns (response, from_cache).
    Only complete (non-truncated), error-free responses are stored."""
    if not settings.llm_dev_cache_active:
        resp = await cli.chat(
            messages, model, response_format=response_format,
            temperature=temperature, max_tokens=max_tokens,
        )
        return resp, False
    key = _cache_key(
        model, messages, temperature=temperature, max_tokens=max_tokens,
        response_format=response_format,
    )
    hit = _cache_get(db, key)
    if hit is not None:
        activity.touch()  # a replay is progress too
        return hit, True
    resp = await cli.chat(
        messages, model, response_format=response_format,
        temperature=temperature, max_tokens=max_tokens,
    )
    if _finish_reason(resp) != "length":
        _cache_put(db, key, model, prompt_sha, resp)
    return resp, False


class OpenRouterError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        upstream_code: int | None = None,
        truncated: bool = False,
        partial: bool = False,
        output_head: str = "",
    ):
        super().__init__(message)
        self.transient = transient
        self.upstream_code = upstream_code
        # True when the model hit max_tokens — callers can fall back to a
        # two-phase strategy instead of just bubbling up.
        self.truncated = truncated
        # True when output tokens had already arrived when the call failed.
        # Such a call is never retried automatically (it would bill twice and
        # hide a provider fault); `output_head` keeps what was received for
        # forensics (ModelCall.output_head).
        self.partial = partial
        self.output_head = output_head


_TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504, 524}
_OUTPUT_HEAD_CHARS = 8000


@dataclass
class _StreamState:
    """What one streamed attempt has received so far — read by the retry loop
    to decide partial/transient and to salvage the output head."""

    got_data: bool = False
    parts: list[str] = field(default_factory=list)
    first_token_ms: int | None = None
    t0: float = field(default_factory=time.monotonic)
    last_content_mono: float = field(default_factory=time.monotonic)
    usage: dict[str, Any] | None = None  # the trailing usage chunk, when it arrived

    @property
    def partial(self) -> bool:
        return bool(self.parts)

    def head(self) -> str:
        return "".join(self.parts)[:_OUTPUT_HEAD_CHARS]


class OpenRouterClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key or settings.openrouter_api_key
        self.base_url = (base_url or settings.openrouter_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise OpenRouterError(
                "OPENROUTER_API_KEY is not set. Configure it in .env to enable AI calls."
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": settings.openrouter_referer,
            "X-Title": settings.openrouter_app_name,
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        response_format: dict[str, Any] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Streamed chat completion, returned in the non-streaming response
        shape: ``{"choices": [{"message": {"content"}, "finish_reason"}],
        "usage": {...}, "_meta": {"first_token_ms", "stream_ms"}}``.

        Deadlines are liveness-based (see Settings): a dead socket
        (LLM_STREAM_IDLE_S), a live socket with no output or reasoning token
        (LLM_CONTENT_SILENCE_S — off by default, hidden reasoning is
        indistinguishable from a stall), and a hard ceiling (LLM_CALL_MAX_S).
        Transient failures are retried once, but only while no output token
        has arrived; after that the error is raised with ``partial=True``.
        Cancelling the awaiting task closes the stream, which stops
        generation and billing on providers that support it.
        """
        if max_tokens is None:
            max_tokens = settings.llm_budget_small
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if response_format:
            body["response_format"] = response_format
        timeout = httpx.Timeout(
            connect=settings.llm_connect_timeout_s,
            read=settings.llm_stream_idle_s,
            write=30.0,
            pool=settings.llm_connect_timeout_s,
        )

        last_err: OpenRouterError | None = None
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(2):
                state = _StreamState()
                try:
                    async with asyncio.timeout(settings.llm_call_max_s):
                        return await self._stream_once(client, body, state)
                except TimeoutError:
                    # asyncio.timeout expired: the hard ceiling. Never retried.
                    raise OpenRouterError(
                        f"OpenRouter call exceeded LLM_CALL_MAX_S="
                        f"{settings.llm_call_max_s:.0f}s (model {model}, "
                        f"{len(state.parts)} content chunks received) — raise the "
                        "ceiling or split the work",
                        partial=state.partial,
                        output_head=state.head(),
                    )
                except httpx.TimeoutException as e:
                    if state.partial:
                        raise OpenRouterError(
                            f"OpenRouter stream stalled after output started: no bytes "
                            f"for LLM_STREAM_IDLE_S={settings.llm_stream_idle_s:.0f}s "
                            f"({type(e).__name__})",
                            partial=True,
                            output_head=state.head(),
                        )
                    waited = time.monotonic() - state.t0
                    _logger.warning(
                        "OpenRouter read timeout before any output token after %.0fs "
                        "(model %s, attempt %d) — LLM_STREAM_IDLE_S=%.0f",
                        waited, model, attempt + 1, settings.llm_stream_idle_s,
                    )
                    last_err = OpenRouterError(
                        f"Network timeout talking to OpenRouter before any output "
                        f"({type(e).__name__} after {waited:.0f}s)",
                        transient=True,
                    )
                except httpx.TransportError as e:
                    if state.partial:
                        raise OpenRouterError(
                            f"OpenRouter connection dropped mid-stream: "
                            f"{type(e).__name__}: {e}",
                            partial=True,
                            output_head=state.head(),
                        )
                    last_err = OpenRouterError(
                        f"Network error talking to OpenRouter: {type(e).__name__}: {e}",
                        transient=True,
                    )
                except OpenRouterError as e:
                    last_err = e

                if not last_err.transient or last_err.partial or attempt == 1:
                    raise last_err
                await asyncio.sleep(1.5)
        raise last_err  # pragma: no cover — loop always returns or raises

    async def _stream_once(
        self, client: httpx.AsyncClient, body: dict[str, Any], state: _StreamState
    ) -> dict[str, Any]:
        """One streamed attempt: parse the SSE stream, reassemble the message.

        Wire facts (OpenRouter docs): ``data: {json}`` events, keepalive
        comment lines ``: OPENROUTER PROCESSING`` at an undocumented cadence,
        ``data: [DONE]`` sentinel; ``finish_reason`` on the last content chunk
        and again on the trailing usage chunk (which carries one choice with an
        empty delta); usage always on that final chunk; a mid-stream failure
        arrives as a chunk with a top-level ``error`` and ``finish_reason:
        "error"`` after which ``[DONE]`` may never come.
        """
        url = f"{self.base_url}/chat/completions"
        try:
            return await self._stream_body(client, url, body, state)
        finally:
            # Observation only: fold this attempt's streamed-token estimate
            # into the ambient task (exact when the usage chunk arrived).
            activity.attempt_finished(state.usage)

    async def _stream_body(
        self, client: httpx.AsyncClient, url: str, body: dict[str, Any], state: _StreamState
    ) -> dict[str, Any]:
        finish: str | None = None
        usage: dict[str, Any] | None = None
        resp_id: str | None = None
        resp_model: str | None = None
        async with client.stream("POST", url, headers=self._headers(), json=body) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", "replace")[:500]
                raise OpenRouterError(
                    f"OpenRouter {resp.status_code}: {text}",
                    transient=resp.status_code in _TRANSIENT_HTTP_CODES,
                    upstream_code=resp.status_code,
                )
            async for raw in resp.aiter_lines():
                now = time.monotonic()
                activity.touch()
                silence = now - state.last_content_mono
                if silence > settings.llm_content_silence_s:
                    raise OpenRouterError(
                        f"OpenRouter stream produced no output or reasoning token for "
                        f"{silence:.0f}s (LLM_CONTENT_SILENCE_S={settings.llm_content_silence_s:.0f}; "
                        f"model {body.get('model')}) — the generation looks stuck (a model that "
                        "reasons hidden can look like this; raise the limit or the ceiling)",
                        partial=state.partial,
                        output_head=state.head(),
                    )
                line = raw.strip()
                if not line or line.startswith(":"):
                    continue  # event delimiter / keepalive comment
                if not line.startswith("data:"):
                    continue  # event:/id:/retry: fields are not used
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError as e:
                    raise OpenRouterError(
                        f"Malformed SSE chunk from OpenRouter: {e}; got: {payload[:200]}",
                        transient=not state.partial,
                        partial=state.partial,
                        output_head=state.head(),
                    )
                state.got_data = True
                if not isinstance(chunk, dict):
                    continue
                err = chunk.get("error")
                if err:
                    code = err.get("code") if isinstance(err, dict) else None
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise OpenRouterError(
                        f"OpenRouter upstream error {code}: {msg}",
                        transient=(
                            isinstance(code, int)
                            and code in _TRANSIENT_HTTP_CODES
                            and not state.partial
                        ),
                        upstream_code=code if isinstance(code, int) else None,
                        partial=state.partial,
                        output_head=state.head(),
                    )
                resp_id = chunk.get("id") or resp_id
                resp_model = chunk.get("model") or resp_model
                if chunk.get("usage"):
                    usage = chunk["usage"]
                    state.usage = usage
                for choice in chunk.get("choices") or []:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    text = delta.get("content") if isinstance(delta, dict) else None
                    if text:
                        if state.first_token_ms is None:
                            state.first_token_ms = int((now - state.t0) * 1000)
                            activity.note_first_token(state.first_token_ms)
                        state.parts.append(text)
                        state.last_content_mono = now
                        activity.note_delta(len(text))
                    elif isinstance(delta, dict) and (
                        delta.get("reasoning") or delta.get("reasoning_details")
                    ):
                        # A thinking model that streams its reasoning is
                        # working, not stuck — count it for the silence tier
                        # (never as content).
                        state.last_content_mono = now
                        r = delta.get("reasoning")
                        activity.note_delta(len(r) if isinstance(r, str) else 1, reasoning=True)
                    fr = choice.get("finish_reason")
                    if fr:
                        finish = fr
                    if fr == "error":
                        # Provider fault without an error envelope (seen on
                        # glm-5.3-flash, 0 tokens after 8 s): safe to retry
                        # once while nothing has been consumed.
                        raise OpenRouterError(
                            "OpenRouter provider reported finish_reason=error",
                            transient=not state.partial,
                            partial=state.partial,
                            output_head=state.head(),
                        )
        if not state.got_data:
            raise OpenRouterError(
                "OpenRouter returned an empty stream (no chunks before [DONE])",
                transient=True,
            )
        return {
            "id": resp_id,
            "model": resp_model,
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "".join(state.parts)},
                    "finish_reason": finish,
                }
            ],
            "usage": usage,
            "_meta": {
                "first_token_ms": state.first_token_ms,
                "stream_ms": int((time.monotonic() - state.t0) * 1000),
            },
        }


def _extract_content(response: dict[str, Any]) -> str:
    try:
        return response["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise OpenRouterError(f"Malformed response: {e}; got: {str(response)[:300]}")


def _finish_reason(response: dict[str, Any]) -> str | None:
    try:
        return response["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


# OpenRouter usage accounting is always on: every non-streaming response carries
# `usage.cost` (credits, USD-denominated), `usage.prompt_tokens_details.cached_tokens`
# and `usage.completion_tokens_details.reasoning_tokens`. The old opt-in
# `usage: {"include": true}` request field is deprecated and a no-op — don't add it.
@dataclass
class _UsageTally:
    """Accumulates usage across every live attempt of one logical call (validation,
    garble and truncation retries all cost real money). Dev-cache hits must not be
    added — see the cache invariant above."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    live_calls: int = 0
    calls_without_cost: int = 0

    def add(self, response: dict[str, Any]) -> None:
        usage = response.get("usage") or {}
        pdet = usage.get("prompt_tokens_details") or {}
        cdet = usage.get("completion_tokens_details") or {}
        self.live_calls += 1
        self.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or 0)
        self.cached_tokens += int(pdet.get("cached_tokens") or 0)
        self.reasoning_tokens += int(cdet.get("reasoning_tokens") or 0)
        cost = usage.get("cost")
        if cost is None:
            self.calls_without_cost += 1
        else:
            self.cost_usd += float(cost)

    @property
    def cost_source(self) -> str:
        """"openrouter" only when every live attempt was metered; "" otherwise."""
        return "openrouter" if self.live_calls and not self.calls_without_cost else ""

    def warn_if_cost_missing(self, purpose: str, model: str) -> None:
        if self.calls_without_cost:
            _logger.warning(
                "OpenRouter response for %s (%s) carried no usage.cost in %d/%d "
                "live call(s); cost_usd is under-reported",
                purpose, model, self.calls_without_cost, self.live_calls,
            )


def _strip_code_fence(content: str) -> str:
    s = content.strip()
    if s.startswith("```"):
        # remove leading ``` or ```json
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _looks_garbled(content: str, data: Any, schema: type[BaseModel]) -> bool:
    """Degenerate model output, as opposed to a schema miss.

    A schema miss is coherent JSON with a wrong/missing field — the stricter
    retry (feed the bad output back with the validator error) fixes it. A
    garbled response (`{"control_codevote": ": "}`) is the model going off
    the rails; feeding it back keeps the model in that mode, so it must be
    retried with fresh context instead. Garbled = not JSON-object-shaped at
    all, or a dict with no recognisable schema field.

    Anything that names at least one schema field is a schema miss, however
    sparse or however many extra fields it carries: schemas whose fields are
    mostly optional (the attestation profile — a pen test legitimately states
    none of the SOC-only fields) yield sparse answers, and fast models pad
    such answers with invented fields. Both are coherent and must reach the
    corrective retry that shows the validator error.
    """
    if not isinstance(data, dict):
        return not content.lstrip().startswith("{")
    known: set[str] = set(schema.model_fields)
    for f in schema.model_fields.values():
        if f.alias:
            known.add(f.alias)
    keys = set(data)
    if not keys:
        return True
    if keys & known:
        return False
    # A single foreign key wrapping an object ({"profile": {...}}) is a
    # coherent shape miss the corrective retry unwraps; anything else with
    # no recognisable field is garble.
    return not (len(keys) == 1 and isinstance(next(iter(data.values())), dict))


async def call_structured(
    db: Session,
    *,
    purpose: str,
    profile: str,
    messages: list[dict[str, Any]],
    schema: type[T],
    assessment_id: int | None = None,
    model_override: str | None = None,
    temperature: float = 0.2,
    max_tokens: int | None = None,
    client: OpenRouterClient | None = None,
) -> T:
    """Call the model and parse JSON into `schema`. Retries once on validation failure."""
    if max_tokens is None:
        max_tokens = settings.llm_budget_small

    model = _resolve_model(profile, model_override)
    cli = client or OpenRouterClient()
    prompt_sha = _hash_prompt(messages)
    response_format = {"type": "json_object"}

    last_err: str = ""
    last_content: str = ""
    output_head: str = ""
    first_token_ms: int | None = None
    started = time.perf_counter()
    usage = _UsageTally()
    ok = False
    transport_err: OpenRouterError | None = None

    validation_retried = False
    garble_retried = False
    truncation_retries = 0
    tokens = max_tokens
    cached = False

    activity.call_started(purpose)
    try:
        while True:
            try:
                resp, from_cache = await _chat_maybe_cached(
                    db, cli, messages, model,
                    response_format=response_format,
                    temperature=temperature,
                    max_tokens=tokens,
                    prompt_sha=prompt_sha,
                )
                cached = cached or from_cache
            except OpenRouterError as e:
                last_err = str(e)
                transport_err = e
                output_head = e.output_head[:_OUTPUT_HEAD_CHARS]
                if e.partial:
                    # Tokens were generated and (possibly) billed but no usage
                    # chunk arrived: count an unmetered live attempt so the
                    # cost-missing warning fires instead of hiding the spend.
                    usage.add({})
                break

            content = _extract_content(resp)
            last_content = content
            if not from_cache:  # a cache hit spent no tokens
                usage.add(resp)
                meta = resp.get("_meta") or {}
                if first_token_ms is None and meta.get("first_token_ms") is not None:
                    first_token_ms = int(meta["first_token_ms"])

            if _finish_reason(resp) == "length":
                # Output was truncated. A truncated response must never be
                # parsed or masked — retry with a doubled output budget (up to
                # the configured ladder depth and ceiling), then fail loudly so
                # callers can segment the work.
                if (
                    truncation_retries < settings.llm_truncation_retries
                    and tokens < settings.llm_truncation_cap
                ):
                    truncation_retries += 1
                    last_err = (
                        f"truncated at max_tokens={tokens}; retried with larger budget"
                    )
                    tokens = min(tokens * 2, settings.llm_truncation_cap)
                    continue
                output_head = content[:_OUTPUT_HEAD_CHARS]
                raise OpenRouterError(
                    f"Structured call '{purpose}' output truncated at max_tokens={tokens} "
                    "even after retrying with a larger budget — the requested output may "
                    "be too large; re-run, reduce the input, or split the work.",
                    truncated=True,
                )

            data: Any = None
            try:
                data = json.loads(_strip_code_fence(content))
                obj = schema.model_validate(data)
                ok = True
                return obj
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = f"{type(e).__name__}: {e}"
                output_head = content[:_OUTPUT_HEAD_CHARS]
                if _looks_garbled(content, data, schema):
                    if garble_retried:
                        # Garbled twice: fail loudly. Never feed garbage into
                        # the schema retry — it would only reproduce it.
                        last_err = f"garbled output twice: {last_err}"
                        break
                    # Degenerate output: retry once with the ORIGINAL messages.
                    garble_retried = True
                    last_err = (
                        "garbled output; retried with fresh context. "
                        f"First attempt: {last_err[:300]}"
                    )
                    continue
                if not validation_retried:
                    validation_retried = True
                    # Stricter retry — append the model's bad output and the validator error.
                    messages = list(messages) + [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "Your previous response did not match the required JSON schema. "
                                f"Validator error:\n{last_err}\n\n"
                                "Return ONLY a single JSON object that conforms to the schema. "
                                "Do not include markdown, prose, or code fences."
                            ),
                        },
                    ]
                    continue
                break

        if transport_err is not None:
            raise transport_err
        raise OpenRouterError(
            f"Structured call '{purpose}' failed validation after retry: {last_err}"
        )
    except asyncio.CancelledError:
        last_err = "cancelled"
        output_head = output_head or last_content[:_OUTPUT_HEAD_CHARS]
        raise
    finally:
        activity.call_finished()
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage.warn_if_cost_missing(purpose, model)
        try:
            db.add(
                ModelCall(
                    assessment_id=assessment_id,
                    purpose=purpose,
                    profile=profile,
                    model_id=model,
                    prompt_sha=prompt_sha,
                    latency_ms=latency_ms,
                    first_token_ms=first_token_ms,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    cost_usd=usage.cost_usd,
                    cost_source=usage.cost_source,
                    ok=ok,
                    error="" if ok else (last_err or "unknown")[:2000],
                    output_head=None if ok else (output_head or None),
                    cached=cached,
                )
            )
            db.commit()
        except Exception:
            db.rollback()


async def call_text(
    db: Session,
    *,
    purpose: str,
    profile: str,
    messages: list[dict[str, Any]],
    assessment_id: int | None = None,
    model_override: str | None = None,
    temperature: float = 0.4,
    max_tokens: int | None = None,
    client: OpenRouterClient | None = None,
) -> str:
    """Plain text call. No JSON validation — used for narrative writing only."""
    if max_tokens is None:
        max_tokens = settings.llm_budget_small
    model = _resolve_model(profile, model_override)
    cli = client or OpenRouterClient()
    prompt_sha = _hash_prompt(messages)
    started = time.perf_counter()
    usage = _UsageTally()
    ok = False
    err = ""
    content = ""
    output_head: str = ""
    first_token_ms: int | None = None
    cached = False
    activity.call_started(purpose)
    try:
        try:
            tokens = max_tokens
            truncation_retries = 0
            while True:
                resp, from_cache = await _chat_maybe_cached(
                    db, cli, messages, model, response_format=None,
                    temperature=temperature, max_tokens=tokens, prompt_sha=prompt_sha,
                )
                cached = cached or from_cache
                content = _extract_content(resp)
                if not from_cache:
                    usage.add(resp)
                    meta = resp.get("_meta") or {}
                    if first_token_ms is None and meta.get("first_token_ms") is not None:
                        first_token_ms = int(meta["first_token_ms"])
                if _finish_reason(resp) == "length":
                    # A cut-off narrative must never be persisted silently.
                    if (
                        truncation_retries < settings.llm_truncation_retries
                        and tokens < settings.llm_truncation_cap
                    ):
                        truncation_retries += 1
                        tokens = min(tokens * 2, settings.llm_truncation_cap)
                        continue
                    output_head = content[:_OUTPUT_HEAD_CHARS]
                    raise OpenRouterError(
                        f"Text call '{purpose}' output truncated at max_tokens={tokens} "
                        "even after retrying with a larger budget — re-run or reduce "
                        "the input.",
                        truncated=True,
                    )
                ok = True
                return content
        except OpenRouterError as e:
            err = str(e)
            output_head = output_head or e.output_head[:_OUTPUT_HEAD_CHARS]
            if e.partial:
                usage.add({})  # unmetered live attempt → cost-missing warning
            raise
        except asyncio.CancelledError:
            err = "cancelled"
            raise
    finally:
        activity.call_finished()
        latency_ms = int((time.perf_counter() - started) * 1000)
        usage.warn_if_cost_missing(purpose, model)
        try:
            db.add(
                ModelCall(
                    assessment_id=assessment_id,
                    purpose=purpose,
                    profile=profile,
                    model_id=model,
                    prompt_sha=prompt_sha,
                    latency_ms=latency_ms,
                    first_token_ms=first_token_ms,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    cost_usd=usage.cost_usd,
                    cost_source=usage.cost_source,
                    ok=ok,
                    error=err[:2000],
                    output_head=None if ok else (output_head or None),
                    cached=cached,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
