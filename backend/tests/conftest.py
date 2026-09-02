"""Test configuration: fresh SQLite DB per test, plus a fake OpenRouter client."""

from __future__ import annotations

import json
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Iterator

import pytest

# Point to a temp DB before any app code imports settings
_tmp = Path(tempfile.mkdtemp(prefix="tprm-tests-"))
os.environ["DB_PATH"] = str(_tmp / "test.sqlite")
os.environ["DATA_DIR"] = str(_tmp)
os.environ["STORAGE_DIR"] = str(_tmp / "storage")
os.environ["OPENROUTER_API_KEY"] = "fake-test-key"


@pytest.fixture()
def fresh_db() -> Iterator[None]:
    """Re-initialise the DB for every test."""
    from app.config import settings
    from app.db import get_engine, init_db
    # Drop pooled connections before unlinking the file to avoid stale fds.
    get_engine().dispose()
    if settings.db_path.exists():
        settings.db_path.unlink()
    init_db()
    yield
    get_engine().dispose()


class _Truncated:
    """Marker wrapper: this canned response was cut off at max_tokens."""

    def __init__(self, content: str):
        self.content = content


class FakeOpenRouterClient:
    """Returns canned JSON/text responses in FIFO order."""

    def __init__(self, queue: list[Any] | None = None):
        self.queue: deque = deque(queue or [])
        self.calls: list[dict] = []

    def push(self, response: Any) -> None:
        self.queue.append(response)

    def push_json(self, obj: dict | list) -> None:
        self.queue.append(json.dumps(obj))

    def push_truncated(self, content: str = "") -> None:
        """Queue a response whose finish_reason is `length` (truncated output)."""
        self.queue.append(_Truncated(content))

    async def chat(
        self,
        messages: list[dict],
        model: str,
        *,
        response_format: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> dict:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_format": response_format,
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
        )
        if not self.queue:
            raise AssertionError(
                f"FakeOpenRouterClient out of canned responses (call #{len(self.calls)} for {model})"
            )
        content = self.queue.popleft()
        finish_reason = "stop"
        if isinstance(content, _Truncated):
            finish_reason = "length"
            content = content.content
        if not isinstance(content, str):
            content = json.dumps(content)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                }
            ],
            # Mirrors OpenRouter's always-on usage accounting (cost in USD credits).
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cost": 0.0123,
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
        }


@pytest.fixture()
def fake_client():
    return FakeOpenRouterClient()

