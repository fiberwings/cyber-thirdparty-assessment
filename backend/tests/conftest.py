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


class _Filtered:
    """Marker wrapper: the provider's content filter cut this canned response."""

    def __init__(self, content: str, native: str, provider: str):
        self.content = content
        self.native = native
        self.provider = provider


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

    def push_filtered(self, content: Any, native: str = "sensitive", provider: str = "X") -> None:
        """Queue a response the provider's content filter cut: normalised
        finish_reason `stop`, native reason `native` (OpenRouter's shape)."""
        self.queue.append(_Filtered(content if isinstance(content, str) else json.dumps(content), native, provider))

    async def chat(
        self,
        messages: list[dict],
        model: str,
        *,
        response_format: dict | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict:
        from app import activity

        activity.touch()  # the real client pings the ambient task per SSE line
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "response_format": response_format,
                "max_tokens": max_tokens,
            }
        )
        if not self.queue:
            raise AssertionError(
                f"FakeOpenRouterClient out of canned responses (call #{len(self.calls)} for {model})"
            )
        content = self.queue.popleft()
        finish_reason = "stop"
        native_finish = "stop"
        provider = "Fake"
        if isinstance(content, _Truncated):
            finish_reason = "length"
            content = content.content
        elif isinstance(content, _Filtered):
            native_finish, provider = content.native, content.provider
            content = content.content
        if not isinstance(content, str):
            content = json.dumps(content)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish_reason,
                    "native_finish_reason": native_finish,
                }
            ],
            "provider": provider,
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


# ---------- Workflow helpers ----------
#
# The workflow guards (app.workflow) refuse a step until every upstream step
# is done and current. Tests that target one endpoint stamp the durable state
# a real run leaves behind instead of replaying the whole pipeline.

WORKFLOW_STEPS = ("scoping", "scenarios", "evidence", "correlation", "analysis", "narratives")


def advance_workflow(aid: int, through: str, *, with_document: bool = True) -> None:
    """Bring assessment `aid` to the state where every step up to and
    including `through` is done and current.

    scoping     description sufficient + force_continued
    scenarios   one description-sourced scenario with one control, phase done
    evidence    one parsed + extracted document (unless with_document=False)
    correlation / analysis / narratives   phase markers done
    """
    from datetime import datetime

    from app.db import SessionLocal
    from app.models import Assessment, Document, ExpectedControl, Scenario, ServiceDescription
    from app.tasks import mark_phase_done

    if through not in WORKFLOW_STEPS:
        raise ValueError(f"unknown step {through!r}")
    upto = WORKFLOW_STEPS.index(through)
    with SessionLocal() as db:
        a = db.get(Assessment, aid)
        assert a is not None
        if a.description is None:
            db.add(ServiceDescription(assessment_id=aid, text="SaaS vendor processing PII."))
            db.flush()
            db.refresh(a)
        a.description.is_sufficient = True
        a.force_continued = True
        if upto >= 1 and not a.scenarios:
            s = Scenario(
                assessment_id=aid,
                code="DATA_LEAK",
                name="Data leakage",
                description="Vendor mishandles PII.",
                source="description",
                inherent_impact=3,
                inherent_likelihood=3,
                residual_impact=3,
                residual_likelihood=3,
                score_band="High",
            )
            db.add(s)
            db.flush()
            db.add(
                ExpectedControl(
                    scenario_id=s.id,
                    code="ENC.REST",
                    name="Encryption at rest",
                    description="",
                    weight=1.0,
                    rationale="",
                )
            )
        if upto >= 2 and with_document and not a.documents:
            db.add(
                Document(
                    assessment_id=aid,
                    kind="policy",
                    filename="policy.txt",
                    mime="text/plain",
                    sha256="0" * 64,
                    size_bytes=10,
                    parsed_at=datetime.utcnow(),
                    weakness_extracted_at=datetime.utcnow(),
                )
            )
        db.commit()
    if upto >= 1:
        mark_phase_done(aid, "scenarios_generation")
    if upto >= 3:
        mark_phase_done(aid, "cross_correlation")
    if upto >= 4:
        mark_phase_done(aid, "gap_analysis")
    if upto >= 5:
        mark_phase_done(aid, "narratives")


def seed_running_task(aid: int, kind: str, task_id: str, *, phase: str | None = None) -> None:
    """A running task record (plus, for phase-tracked kinds, the phase marker
    pointing at it) — the durable state a click leaves behind while its job
    is executing. `registry.get()` resolves it from the table."""
    from app.db import SessionLocal
    from app.models import TaskRecord
    from app.tasks import mark_phase_started

    with SessionLocal() as db:
        db.add(
            TaskRecord(
                id=task_id,
                kind=kind,
                assessment_id=aid,
                status="running",
                progress=0.2,
                detail="running",
                error="",
            )
        )
        db.commit()
    if phase:
        mark_phase_started(aid, phase, task_id)
