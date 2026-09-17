"""Tiny script that exercises both model profiles against real provider keys.

Each profile's default model is sent through whatever dialect its ref names
(bare id → OpenRouter, `azure:<deployment>` → Azure OpenAI,
`foundry:<deployment>` → Azure AI Foundry Models), so this doubles as a check
that the credentials, endpoint and deployment metadata are wired correctly.

Usage (from `backend/`):
    OPENROUTER_API_KEY=... .venv/bin/python scripts/smoke_llm.py
    MODEL_FAST=azure:gpt5-mini AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=... \\
        .venv/bin/python scripts/smoke_llm.py
"""

from __future__ import annotations

import asyncio
import sys
import time

from app.ai.providers import check_provider_config, parse_model_ref
from app.ai.router import LLMClient
from app.config import settings


async def _ping(profile: str, model: str) -> None:
    client = LLMClient()
    started = time.perf_counter()
    resp = await client.chat(
        messages=[
            {"role": "system", "content": "Reply with exactly one word."},
            {"role": "user", "content": f"Say {profile.upper()}."},
        ],
        model=model,
        temperature=0.0,
        max_tokens=64,
    )
    latency = (time.perf_counter() - started) * 1000
    choice = resp["choices"][0]
    content = choice["message"]["content"].strip()
    meta = resp.get("_meta", {})
    print(
        f"  [{profile}] {model}: {content!r} ({latency:.0f} ms)\n"
        f"      dialect={meta.get('dialect')} provider={resp.get('provider')} "
        f"finish={choice.get('finish_reason')}/{choice.get('native_finish_reason')} "
        f"temperature_sent={meta.get('temperature_sent')}\n"
        f"      usage={resp.get('usage')}"
    )


async def main() -> int:
    models = {"fast": settings.model_fast, "reasoner": settings.model_reasoner}
    problems = check_provider_config(models.values(), defaults=True)
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return 1
    for profile, model in models.items():
        print(f"Pinging {parse_model_ref(model).scheme} for the {profile} profile...")
        try:
            await _ping(profile, model)
        except Exception as e:
            print(f"FAILED: {e}", file=sys.stderr)
            return 2
    print("OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
