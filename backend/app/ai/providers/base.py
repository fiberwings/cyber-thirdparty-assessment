"""Provider dialects: the wire-level differences between LLM backends.

Everything above the wire — the truncation ladder, content-filter detection,
schema/garble retries, per-attempt forensics, usage accounting and the
liveness deadlines — lives once in `app.ai.router` and consumes one
normalised response shape. A `Dialect` only knows how to *shape a request*
for its backend, *label* what came back, and *classify* an HTTP failure.
Adding a provider means adding a dialect, never touching the ladder.

A `ModelRef` is the parsed form of a configured model string. Model refs
stay plain strings everywhere they are stored or transported (env, the
per-assessment overrides, `model_call.model_id`, the benchmark); they are
parsed once per call by the router (see `registry.parse_model_ref`).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ModelRef:
    raw: str          # as configured / persisted, e.g. "azure:gpt5-prod"
    scheme: str       # "openrouter" | "azure" | "foundry"
    wire: str         # what goes in body["model"] (OpenRouter id / deployment name)
    canonical: str    # MODEL_CAPS key (== wire for OpenRouter ids)
    # The deployment rejects any `temperature` but its default (Azure OpenAI
    # reasoning models). Set only by an explicit AZURE_DEPLOYMENT_META flag —
    # an explicit, logged sampling change, never inferred (CLAUDE.md).
    fixed_temperature: bool = False


class LLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        upstream_code: int | None = None,
        truncated: bool = False,
        partial: bool = False,
        output_head: str = "",
        filtered: bool = False,
        retry_after_s: float | None = None,
        rate_limit: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.transient = transient
        self.upstream_code = upstream_code
        # True when the provider's content filter stopped the generation
        # (see router._content_filtered): the output is incomplete but
        # splitting the input will not help — route around the provider or
        # change the deployment's filter policy instead.
        self.filtered = filtered
        # True when the model hit max_tokens — callers can fall back to a
        # two-phase strategy instead of just bubbling up.
        self.truncated = truncated
        # True when output tokens had already arrived when the call failed.
        # Such a call is never retried automatically (it would bill twice and
        # hide a provider fault); `output_head` keeps what was received for
        # forensics (ModelCall.output_head).
        self.partial = partial
        self.output_head = output_head
        # Provider-suggested wait (Retry-After on 429/503) before the next
        # pre-first-token retry; capped by LLM_RETRY_AFTER_CAP_S.
        self.retry_after_s = retry_after_s
        # `x-ratelimit-*` headers of a rate-limited response (Azure reports the
        # deployment's TPM/RPM limits, OpenRouter its credit-based ones) — the
        # diagnosis of a 429 storm is "concurrency × reserved budget exceeds
        # this", so the router logs and records them.
        self.rate_limit = rate_limit or {}
        # The router's transient retries that preceded this failure, one
        # record each (see router.LLMClient.chat) — surfaced on
        # `model_call.attempts_json` so a lost stage shows what was tried.
        self.retries: list[dict[str, Any]] = []


TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504, 524}
# Statuses whose Retry-After header is a real backoff hint (RFC 9110 §10.2.3
# defines it for 503 and 429/3xx; Azure and OpenRouter send it on both).
RETRY_AFTER_HTTP_CODES = {429, 503}


def parse_error_json(text: str) -> dict[str, Any]:
    """The `error` object of a provider error body, or {} when it is not JSON."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    err = data.get("error") if isinstance(data, dict) else None
    return err if isinstance(err, dict) else {}


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Numeric Retry-After (seconds) or retry-after-ms, else None (HTTP-date
    forms are ignored — the capped default wait applies)."""
    if not headers:
        return None
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000.0)
        except ValueError:
            pass
    s = headers.get("retry-after")
    if s:
        try:
            return max(0.0, float(s))
        except ValueError:
            return None
    return None


def rate_limit_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Every `x-ratelimit-*` header, lower-cased and stripped of the prefix
    (`limit-tokens`, `remaining-requests`, …), for 429 forensics."""
    if not headers:
        return {}
    out: dict[str, str] = {}
    for k, v in headers.items():
        lk = k.lower()
        if lk.startswith("x-ratelimit-"):
            out[lk[len("x-ratelimit-"):]] = str(v)[:64]
    return out


class Dialect(ABC):
    """One backend's wire conventions. Instances are cheap and stateless;
    they read `settings` at call time so configuration can change under
    tests."""

    name: str                # "openrouter" | "azure-openai" | "azure-foundry"
    meters_cost: bool        # response `usage.cost` is expected on every reply
    # Whether the stream carries bytes (keepalive comments) while the model
    # thinks. Without them a raw read timeout cannot tell a dead socket from
    # a reasoning model at work, so the router uses the no-keepalive idle
    # limit and heartbeats the task watchdog itself.
    sends_keepalives: bool

    @abstractmethod
    def credentials_missing(self) -> str | None:
        """None when the dialect is usable, else a message naming the
        missing setting."""

    @abstractmethod
    def endpoint(self, ref: ModelRef) -> str: ...

    @abstractmethod
    def headers(self) -> dict[str, str]: ...

    @abstractmethod
    def build_body(
        self,
        ref: ModelRef,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """The streaming request body. Must never drop the output budget or
        `response_format`; `temperature` is omitted only for
        `ref.fixed_temperature`."""

    def provider_label(self, chunk: dict[str, Any], ref: ModelRef) -> str | None:
        """Who served this chunk, for `model_call.provider` (≤ 60 chars).
        OpenRouter names the upstream host; Azure names the resource."""
        return None

    def choice_finish(self, choice: dict[str, Any]) -> tuple[str | None, str | None]:
        """(finish_reason, native_finish_reason) of a streamed choice. The
        native reason is what `router._content_filtered` inspects first."""
        fr = choice.get("finish_reason")
        return fr, choice.get("native_finish_reason")

    def http_error(
        self, status: int, text: str, headers: Mapping[str, str] | None = None
    ) -> LLMError:
        """Classify a non-2xx response. Default: transient on the usual
        gateway/rate-limit codes, honouring Retry-After (429/503) and keeping
        the rate-limit headers of a 429."""
        return LLMError(
            f"{self.name} {status}: {text}",
            transient=status in TRANSIENT_HTTP_CODES,
            upstream_code=status,
            retry_after_s=retry_after_seconds(headers) if status in RETRY_AFTER_HTTP_CODES else None,
            rate_limit=rate_limit_headers(headers) if status == 429 else None,
        )

    @abstractmethod
    def filter_hint(self) -> str:
        """Remedy text appended to a `filtered=True` failure."""
