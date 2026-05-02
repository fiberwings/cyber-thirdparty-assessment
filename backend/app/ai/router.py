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

import hashlib
import json
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


def _hash_prompt(messages: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(messages, sort_keys=True).encode()).hexdigest()


class OpenRouterError(RuntimeError):
    pass


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
        max_tokens: int = 2048,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            body["response_format"] = response_format

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
            )
            if resp.status_code >= 400:
                raise OpenRouterError(f"OpenRouter {resp.status_code}: {resp.text[:500]}")
            return resp.json()


def _extract_content(response: dict[str, Any]) -> str:
    try:
        return response["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise OpenRouterError(f"Malformed response: {e}; got: {str(response)[:300]}")


def _extract_usage(response: dict[str, Any]) -> tuple[int, int]:
    usage = response.get("usage") or {}
    return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def _strip_code_fence(content: str) -> str:
    s = content.strip()
    if s.startswith("```"):
        # remove leading ``` or ```json
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


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
    max_tokens: int = 2048,
    client: OpenRouterClient | None = None,
) -> T:
    """Call the model and parse JSON into `schema`. Retries once on validation failure."""

    model = _resolve_model(profile, model_override)
    cli = client or OpenRouterClient()
    prompt_sha = _hash_prompt(messages)
    response_format = {"type": "json_object"}

    last_err: str = ""
    last_content: str = ""
    started = time.perf_counter()
    in_tok = out_tok = 0
    ok = False

    try:
        for attempt in range(2):
            try:
                resp = await cli.chat(
                    messages,
                    model,
                    response_format=response_format,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except OpenRouterError as e:
                last_err = str(e)
                break

            content = _extract_content(resp)
            last_content = content
            i_tok, o_tok = _extract_usage(resp)
            in_tok += i_tok
            out_tok += o_tok

            try:
                data = json.loads(_strip_code_fence(content))
                obj = schema.model_validate(data)
                ok = True
                return obj
            except (json.JSONDecodeError, ValidationError) as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt == 0:
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

        raise OpenRouterError(
            f"Structured call '{purpose}' failed validation after retry: {last_err}"
        )
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            db.add(
                ModelCall(
                    assessment_id=assessment_id,
                    purpose=purpose,
                    profile=profile,
                    model_id=model,
                    prompt_sha=prompt_sha,
                    latency_ms=latency_ms,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    ok=ok,
                    error="" if ok else (last_err or "unknown")[:2000],
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
    max_tokens: int = 1024,
    client: OpenRouterClient | None = None,
) -> str:
    """Plain text call. No JSON validation — used for narrative writing only."""
    model = _resolve_model(profile, model_override)
    cli = client or OpenRouterClient()
    prompt_sha = _hash_prompt(messages)
    started = time.perf_counter()
    in_tok = out_tok = 0
    ok = False
    err = ""
    content = ""
    try:
        try:
            resp = await cli.chat(
                messages, model, temperature=temperature, max_tokens=max_tokens
            )
            content = _extract_content(resp)
            in_tok, out_tok = _extract_usage(resp)
            ok = True
            return content
        except OpenRouterError as e:
            err = str(e)
            raise
    finally:
        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            db.add(
                ModelCall(
                    assessment_id=assessment_id,
                    purpose=purpose,
                    profile=profile,
                    model_id=model,
                    prompt_sha=prompt_sha,
                    latency_ms=latency_ms,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    ok=ok,
                    error=err[:2000],
                )
            )
            db.commit()
        except Exception:
            db.rollback()
