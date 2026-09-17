"""OpenRouter: OpenAI-compatible SSE with a few extras the router relies on.

Wire facts (OpenRouter docs): `data: {json}` events, keepalive comment lines
`: OPENROUTER PROCESSING` while the model thinks, `data: [DONE]` sentinel;
each chunk carries `provider` (the upstream host that served it) and each
choice a `native_finish_reason` — the host's own stop reason, which is what
exposes a content-filter cut that OpenRouter normalises to `stop`. Usage
accounting is always on: the final chunk's `usage` carries `cost` (USD
credits), `prompt_tokens_details.cached_tokens` and
`completion_tokens_details.reasoning_tokens`. The old opt-in
`usage: {"include": true}` request field is deprecated and a no-op.
"""

from __future__ import annotations

from typing import Any

from app.config import settings

from .base import Dialect, ModelRef


class OpenRouterDialect(Dialect):
    name = "openrouter"
    meters_cost = True
    sends_keepalives = True

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self._api_key = api_key
        self._base_url = base_url

    @property
    def api_key(self) -> str:
        return self._api_key or settings.openrouter_api_key

    @property
    def base_url(self) -> str:
        return (self._base_url or settings.openrouter_base_url).rstrip("/")

    def credentials_missing(self) -> str | None:
        if not self.api_key:
            return "OPENROUTER_API_KEY is not set. Configure it in .env to enable AI calls."
        return None

    def endpoint(self, ref: ModelRef) -> str:
        return f"{self.base_url}/chat/completions"

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": settings.openrouter_referer,
            "X-Title": settings.openrouter_app_name,
            "Content-Type": "application/json",
        }

    def build_body(
        self,
        ref: ModelRef,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, Any] | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": ref.wire,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if response_format:
            body["response_format"] = response_format
        if settings.provider_ignore_list:
            body["provider"] = {"ignore": settings.provider_ignore_list}
        return body

    def provider_label(self, chunk: dict[str, Any], ref: ModelRef) -> str | None:
        p = chunk.get("provider")
        return p if isinstance(p, str) and p else None

    def filter_hint(self) -> str:
        return "exclude that provider (OPENROUTER_PROVIDER_IGNORE) or use another model"
