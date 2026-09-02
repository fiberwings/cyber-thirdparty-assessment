"""OpenRouter client + structured-output helper.

Profiles:
    fast      → cheap/fast model (default Haiku 4.5) for Q&A loop, ingestion,
                document classification.
    reasoner  → heavy reasoner (default Opus 4.7; GPT-5 selectable) for scenario
                generation, gap analysis, weakness synthesis.

`call_structured` does:
    1. POST /chat/completions with response_format=json_object
    2. Parse JSON, validate against the supplied Pydantic model
    3. On validation failure, retry ONCE with a stricter follow-up message
    4. Persist a ModelCall row regardless of outcome
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

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

    try:
        if db.get(LlmCacheEntry, key) is None:
            db.add(LlmCacheEntry(key=key, model_id=model, prompt_sha=prompt_sha, response_json=response))
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
    timeout = _timeout_for_budget(max_tokens)
    if not settings.llm_dev_cache_active:
        resp = await cli.chat(
            messages, model, response_format=response_format,
            temperature=temperature, max_tokens=max_tokens, timeout=timeout,
        )
        return resp, False
    key = _cache_key(
        model, messages, temperature=temperature, max_tokens=max_tokens,
        response_format=response_format,
    )
    hit = _cache_get(db, key)
    if hit is not None:
        return hit, True
    resp = await cli.chat(
        messages, model, response_format=response_format,
        temperature=temperature, max_tokens=max_tokens, timeout=timeout,
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
    ):
        super().__init__(message)
        self.transient = transient
        self.upstream_code = upstream_code
        # True when the model hit max_tokens — callers can fall back to a
        # two-phase strategy instead of just bubbling up.
        self.truncated = truncated


_TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504, 524}


def _timeout_for_budget(tokens: int) -> float:
    """Per-attempt HTTP timeout sized for a non-streaming response of up to
    `tokens` output tokens: nothing arrives until generation completes, so the
    read timeout must cover the whole generation."""
    return min(
        settings.llm_timeout_max_s,
        settings.llm_timeout_base_s + tokens / settings.llm_assumed_output_tps,
    )


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
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if max_tokens is None:
            max_tokens = settings.llm_budget_small
        if timeout is None:
            timeout = _timeout_for_budget(max_tokens)
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            body["response_format"] = response_format

        last_err: OpenRouterError | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{self.base_url}/chat/completions",
                        headers=self._headers(),
                        json=body,
                    )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = OpenRouterError(
                    f"Network error talking to OpenRouter: {type(e).__name__}: {e}",
                    transient=True,
                )
            else:
                if resp.status_code >= 400:
                    last_err = OpenRouterError(
                        f"OpenRouter {resp.status_code}: {resp.text[:500]}",
                        transient=resp.status_code in _TRANSIENT_HTTP_CODES,
                        upstream_code=resp.status_code,
                    )
                else:
                    data = resp.json()
                    err = data.get("error") if isinstance(data, dict) else None
                    if err:
                        # OpenRouter sometimes returns 200 with an `error` envelope when an
                        # upstream provider fails (e.g. 504 "operation aborted").
                        code = err.get("code") if isinstance(err, dict) else None
                        msg = err.get("message") if isinstance(err, dict) else str(err)
                        last_err = OpenRouterError(
                            f"OpenRouter upstream error {code}: {msg}",
                            transient=isinstance(code, int) and code in _TRANSIENT_HTTP_CODES,
                            upstream_code=code if isinstance(code, int) else None,
                        )
                    else:
                        return data

            if not last_err.transient or attempt == 1:
                raise last_err
            await asyncio.sleep(1.5)

        # Unreachable, but keep mypy happy.
        raise last_err  # type: ignore[misc]


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
    all, or a dict whose keys barely overlap the schema's fields.
    """
    if not isinstance(data, dict):
        return not content.lstrip().startswith("{")
    expected = set(schema.model_fields)
    return len(expected & set(data)) <= len(expected) // 3


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
    started = time.perf_counter()
    usage = _UsageTally()
    ok = False
    transport_err: OpenRouterError | None = None

    validation_retried = False
    garble_retried = False
    truncation_retries = 0
    tokens = max_tokens
    cached = False

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
                break

            content = _extract_content(resp)
            last_content = content
            if not from_cache:  # a cache hit spent no tokens
                usage.add(resp)

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
    finally:
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
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    cost_usd=usage.cost_usd,
                    cost_source=usage.cost_source,
                    ok=ok,
                    error="" if ok else (last_err or "unknown")[:2000],
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
    cached = False
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
                if _finish_reason(resp) == "length":
                    # A cut-off narrative must never be persisted silently.
                    if (
                        truncation_retries < settings.llm_truncation_retries
                        and tokens < settings.llm_truncation_cap
                    ):
                        truncation_retries += 1
                        tokens = min(tokens * 2, settings.llm_truncation_cap)
                        continue
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
            raise
    finally:
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
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cached_tokens=usage.cached_tokens,
                    reasoning_tokens=usage.reasoning_tokens,
                    cost_usd=usage.cost_usd,
                    cost_source=usage.cost_source,
                    ok=ok,
                    error=err[:2000],
                    cached=cached,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
