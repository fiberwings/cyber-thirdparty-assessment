"""Ambient liveness and progress signal for background tasks.

`current_task` is set by the task registry around every job (and inherited
by every coroutine the job fans out to, since contextvars are copied into
child tasks). `touch()` is called by the LLM router on every streamed SSE
line — so a task that is slow but producing tokens keeps proving it is
alive, while a wedged one goes quiet and the watchdog can act.

The other hooks feed the structured progress the UI renders (stage, units,
calls in flight, streamed tokens). Every hook is a no-op without an ambient
task and never raises: observing the work must never break it.
Deliberately dependency-free so both the router and the registry can import
it."""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover
    from app.tasks import TaskHandle

current_task: ContextVar["TaskHandle | None"] = ContextVar("current_task", default=None)


def _on_handle(fn: Callable[["TaskHandle"], Any]) -> None:
    handle = current_task.get()
    if handle is None:
        return
    try:
        fn(handle)
    except Exception:  # liveness must never break the work it observes
        pass


def touch() -> None:
    """Record activity on the ambient task, if any. Never raises."""
    _on_handle(lambda h: h.touch())


# ---- structured progress (called by the job / agents) ----

def plan(stages: list[str]) -> None:
    _on_handle(lambda h: h.plan(stages))


def stage(label: str, *, units_total: int = 0, unit_label: str = "",
          detail: str | None = None) -> None:
    _on_handle(lambda h: h.stage(label, units_total=units_total, unit_label=unit_label, detail=detail))


def advance(done: int | None = None, *, detail: str | None = None) -> None:
    _on_handle(lambda h: h.advance(done, detail=detail))


# ---- per-call telemetry (called by the LLM router) ----

def call_started(purpose: str) -> None:
    _on_handle(lambda h: h.note_call_started(purpose))


def call_finished() -> None:
    _on_handle(lambda h: h.note_call_finished())


def note_delta(n_chars: int, *, reasoning: bool = False) -> None:
    _on_handle(lambda h: h.note_delta(n_chars, reasoning=reasoning))


def note_first_token(ms: int | None) -> None:
    _on_handle(lambda h: h.note_first_token(ms))


def attempt_finished(usage: dict | None) -> None:
    """One streamed attempt ended. With the provider's usage chunk the
    task's token counters become exact; without it the estimate stands."""
    if usage:
        cdet = usage.get("completion_tokens_details") or {}
        completion = usage.get("completion_tokens")
        reasoning = cdet.get("reasoning_tokens") if isinstance(cdet, dict) else None
        _on_handle(lambda h: h.note_attempt_finished(
            int(completion) if completion is not None else None,
            int(reasoning) if reasoning is not None else None,
        ))
    else:
        _on_handle(lambda h: h.note_attempt_finished(None, None))
