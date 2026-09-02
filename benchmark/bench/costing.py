"""Cost and time rollups shared by `bench cost` and the dashboard.

Two kinds of spend are tracked and must never be blended silently:

- **assessment (pipeline)** — what the app under test spent, taken from the
  app's own telemetry (`case_result.tokens_json`). An assessment graded again
  (`bench grade`, mode=grade runs) re-reports the same spend in every run that
  references it; real money was spent once, so grand totals count each
  `assessment_id` once (see `AssessmentLedger`).
- **judge** — what the benchmark itself spent grading (`judge_call.cost_usd`).
  Every call is new spend; never reused.

A figure that contains list-price estimates is flagged `estimated` (rendered
`≈`); one that silently excludes live calls that recorded no cost is flagged
with `missing > 0` (rendered `⚠`) — it is under-reported, not free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

# Pipeline stages in execution order (keys of case_result.timings_json).
STAGE_ORDER = (
    "create",
    "settings",
    "description",
    "scenarios",
    "documents",
    "cross_correlate",
    "gap_analysis",
    "recalculate",
    "narratives",
    "report",
)


@dataclass(frozen=True)
class Money:
    """A USD amount with provenance flags. `usd is None` means *unknown* —
    nothing was priced — which is different from $0."""

    usd: float | None = None
    estimated: bool = False  # contains list-price × tokens estimates, not metered cost
    missing: int = 0  # live calls that recorded no cost — the amount is under-reported
    reused: bool = False  # re-reports spend already counted for another run

    @property
    def known(self) -> bool:
        return self.usd is not None

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            return NotImplemented
        if self.usd is None and other.usd is None:
            usd = None
        else:
            usd = (self.usd or 0.0) + (other.usd or 0.0)
        return Money(
            usd,
            self.estimated or other.estimated,
            self.missing + other.missing,
            self.reused or other.reused,
        )

    def __radd__(self, other):  # lets sum() start from 0
        if other == 0:
            return self
        return NotImplemented


def case_cost(tokens: dict | None) -> Money:
    """Assessment spend from the app telemetry snapshot stored on a case result."""
    if not tokens or "total_cost_usd" not in tokens:
        return Money()
    return Money(
        usd=float(tokens.get("total_cost_usd") or 0.0),
        estimated=bool(tokens.get("total_cost_estimated")),
        missing=int(tokens.get("total_cost_missing") or 0),
    )


def case_time_s(timings: dict | None) -> float | None:
    """Assessment wall time = sum of pipeline stages; None when the case did
    not run the pipeline (grade-mode re-grades store `{}`)."""
    if not timings:
        return None
    return float(sum(v for v in timings.values() if v is not None))


class _JudgeCallLike(Protocol):
    cost_usd: float | None
    cost_source: str
    ok: bool
    latency_ms: int | None


def judge_cost(calls: Iterable[_JudgeCallLike]) -> Money:
    """Σ judge_call.cost_usd. Successful calls with no price count as `missing`
    (delisted model, or cost never reported); failed calls are simply unpriced."""
    usd: float | None = None
    estimated = False
    missing = 0
    for c in calls:
        if c.cost_usd is None:
            missing += int(bool(c.ok))
            continue
        usd = (usd or 0.0) + c.cost_usd
        estimated = estimated or c.cost_source == "estimated"
    return Money(usd, estimated, missing)


def judge_time_s(calls: Iterable[_JudgeCallLike]) -> float | None:
    """Σ judge latency, failed calls included (the time was spent)."""
    ms = [c.latency_ms for c in calls if c.latency_ms is not None]
    return sum(ms) / 1000.0 if ms else None


class AssessmentLedger:
    """Counts each assessment's pipeline spend once across runs.

    `record()` returns True when the assessment was already seen — the caller
    is re-reporting spend counted for an earlier run. Case results with no
    assessment id (the pipeline failed before creating one) are counted
    individually: whatever they spent is real.
    """

    def __init__(self) -> None:
        self.seen: dict[int, Money] = {}
        self.unattributed: list[Money] = []

    def record(self, assessment_id: int | None, cost: Money) -> bool:
        if assessment_id is None:
            self.unattributed.append(cost)
            return False
        if assessment_id in self.seen:
            return True
        self.seen[assessment_id] = cost
        return False

    @property
    def distinct(self) -> int:
        return len(self.seen)

    def total(self) -> Money:
        return sum(self.seen.values(), Money()) + sum(self.unattributed, Money())
