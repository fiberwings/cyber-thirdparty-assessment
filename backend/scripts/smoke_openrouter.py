"""Tiny script that exercises both model profiles against a real OpenRouter key.

Usage (from `backend/`):
    OPENROUTER_API_KEY=... .venv/bin/python scripts/smoke_openrouter.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

from app.ai.router import OpenRouterClient
from app.config import settings


async def _ping(profile: str, model: str) -> None:
    client = OpenRouterClient()
    started = time.perf_counter()
    resp = await client.chat(
        messages=[
            {"role": "system", "content": "Reply with exactly one word."},
            {"role": "user", "content": f"Say {profile.upper()}."},
        ],
        model=model,
        temperature=0.0,
        max_tokens=8,
    )
    latency = (time.perf_counter() - started) * 1000
    content = resp["choices"][0]["message"]["content"].strip()
    usage = resp.get("usage", {})
    print(f"  [{profile}] {model}: {content!r} ({latency:.0f} ms, {usage})")


async def main() -> int:
    if not settings.openrouter_api_key:
        print("Set OPENROUTER_API_KEY in your environment.", file=sys.stderr)
        return 1
    print("Pinging OpenRouter...")
    try:
        await _ping("fast", settings.model_fast)
        await _ping("reasoner", settings.model_reasoner)
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2
    print("OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
