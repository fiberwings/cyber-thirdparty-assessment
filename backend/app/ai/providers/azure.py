"""Azure AI Foundry dialects.

Two surfaces, two schemes:

* `azure:<deployment>` — an **Azure OpenAI** deployment. `POST
  {AZURE_OPENAI_ENDPOINT}/openai/v1/chat/completions` (no api-version; the
  legacy `/openai/deployments/<name>/chat/completions?api-version=` path is
  used when AZURE_OPENAI_API_VERSION is set). A Foundry resource exposes this
  surface too, so OpenAI-family deployments always use `azure:`.
* `foundry:<deployment>` — the **Foundry Models** inference endpoint for
  non-OpenAI models (DeepSeek, Llama, Mistral, Phi, Grok…): `POST
  {AZURE_INFERENCE_ENDPOINT}/models/chat/completions?api-version=`.

Both stream OpenAI-style SSE without keepalive comments. Stream shape (Azure
OpenAI): a leading chunk with `choices: []`, an empty `id` and
`prompt_filter_results`; a role chunk; content chunks each annotated with
`content_filter_results`; a last content chunk with `finish_reason`; a
trailing `{"choices": [], "usage": …}` (only with `stream_options.include_usage`);
`data: [DONE]`. The router's loop already tolerates empty choice lists.

Content filtering differs from OpenRouter in two ways the router must see:
a *completion* cut arrives as `finish_reason: "content_filter"` with the
offending category in `content_filter_results` (surfaced as the native
reason `content_filter:<category>/<severity>`), and a *prompt* can be
rejected outright with HTTP 400 `error.code == "content_filter"`. Both are
`filtered=True`: never parsed, never cached, never silently retried past the
router's single fresh-context retry. Azure does not meter cost.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlsplit

from app.config import settings

from .base import Dialect, LLMError, ModelRef, parse_error_json

_PROVIDER_LABEL_MAX = 60  # model_call.provider is String(60)


def _resource_name(endpoint: str) -> str:
    """`https://acme-eu.openai.azure.com` → `acme-eu` (host without the
    Azure domain suffix, so the label fits the column)."""
    host = urlsplit(endpoint).hostname or endpoint
    return host.split(".", 1)[0] if host else endpoint


def _filtered_category(choice: dict[str, Any]) -> str | None:
    """`<category>/<severity>` of the first filtered content-filter result on a
    choice, else None."""
    results = choice.get("content_filter_results")
    if not isinstance(results, dict):
        return None
    for category, verdict in results.items():
        if isinstance(verdict, dict) and (verdict.get("filtered") or verdict.get("detected")):
            sev = verdict.get("severity")
            return f"{category}/{sev}" if sev else str(category)
    return None


class _AzureDialect(Dialect):
    meters_cost = False
    sends_keepalives = False

    endpoint_setting: str
    key_setting: str

    @property
    def base_endpoint(self) -> str:
        return getattr(settings, self.endpoint_setting.lower()).rstrip("/")

    @property
    def api_key(self) -> str:
        return getattr(settings, self.key_setting.lower())

    def credentials_missing(self) -> str | None:
        if not self.base_endpoint:
            return f"{self.endpoint_setting} is not set. Configure it in .env to use {self.name}."
        if not self.api_key:
            return f"{self.key_setting} is not set. Configure it in .env to use {self.name}."
        return None

    def headers(self) -> dict[str, str]:
        return {"api-key": self.api_key, "Content-Type": "application/json"}

    def provider_label(self, chunk: dict[str, Any], ref: ModelRef) -> str | None:
        return f"{self.name}:{_resource_name(self.base_endpoint)}"[:_PROVIDER_LABEL_MAX]

    def choice_finish(self, choice: dict[str, Any]) -> tuple[str | None, str | None]:
        fr = choice.get("finish_reason")
        native = fr
        cat = _filtered_category(choice)
        if fr == "content_filter" and cat:
            native = f"content_filter:{cat}"[:_PROVIDER_LABEL_MAX]
        return fr, native

    def http_error(
        self, status: int, text: str, headers: Mapping[str, str] | None = None
    ) -> LLMError:
        err = parse_error_json(text)
        code = err.get("code")
        message = str(err.get("message") or text)
        if status == 400 and code == "content_filter":
            # The *prompt* tripped the filter: nothing was generated and a
            # retry with the same evidence cannot pass. Loud, non-transient.
            inner = err.get("innererror") if isinstance(err.get("innererror"), dict) else {}
            verdicts = inner.get("content_filter_result") or {}
            hit = ", ".join(
                k for k, v in verdicts.items()
                if isinstance(v, dict) and (v.get("filtered") or v.get("detected"))
            ) if isinstance(verdicts, dict) else ""
            return LLMError(
                f"{self.name} rejected the prompt: content filter"
                + (f" ({hit})" if hit else "")
                + f" — {message[:300]}. {self.filter_hint()}.",
                upstream_code=status,
                filtered=True,
            )
        if status == 400 and "temperature" in message.lower() and (
            "unsupported" in message.lower() or "not supported" in message.lower()
        ):
            return LLMError(
                f"{self.name} 400: {message[:300]} — this deployment only accepts its "
                "default temperature; add `;temp=fixed` to its AZURE_DEPLOYMENT_META entry "
                "to omit the parameter for it (an explicit sampling change, see .env.example)",
                upstream_code=status,
            )
        return super().http_error(status, text, headers)

    def filter_hint(self) -> str:
        return (
            "adjust the deployment's content-filter policy in Azure AI Foundry "
            "or use another deployment"
        )


class AzureOpenAIDialect(_AzureDialect):
    name = "azure-openai"
    endpoint_setting = "AZURE_OPENAI_ENDPOINT"
    key_setting = "AZURE_OPENAI_API_KEY"

    def endpoint(self, ref: ModelRef) -> str:
        version = settings.azure_openai_api_version.strip()
        if version:
            return (
                f"{self.base_endpoint}/openai/deployments/{ref.wire}/chat/completions"
                f"?api-version={version}"
            )
        return f"{self.base_endpoint}/openai/v1/chat/completions"

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
            # Reasoning deployments reject `max_tokens`; `max_completion_tokens`
            # is accepted by every current chat model and, like OpenRouter's
            # `max_tokens`, bounds visible + reasoning tokens together.
            "max_completion_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if not ref.fixed_temperature:
            body["temperature"] = temperature
        if response_format:
            body["response_format"] = response_format
        return body


class AzureFoundryDialect(_AzureDialect):
    name = "azure-foundry"
    endpoint_setting = "AZURE_INFERENCE_ENDPOINT"
    key_setting = "AZURE_INFERENCE_API_KEY"

    def endpoint(self, ref: ModelRef) -> str:
        return (
            f"{self.base_endpoint}/models/chat/completions"
            f"?api-version={settings.azure_inference_api_version}"
        )

    def headers(self) -> dict[str, str]:
        h = super().headers()
        # Parameters outside the Model Inference schema (stream_options) are
        # rejected unless passed through to the model.
        h["extra-parameters"] = "pass-through"
        return h

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
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if not ref.fixed_temperature:
            body["temperature"] = temperature
        if response_format:
            body["response_format"] = response_format
        return body
