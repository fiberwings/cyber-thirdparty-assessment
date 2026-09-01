"""Scenario generator — runs against the reasoner profile.

Two-phase generation:
    Phase 1 — one reasoner call returns scenario skeletons (no controls).
    Phase 2 — for each skeleton, a focused reasoner call returns its expected
              controls. Phase 2 calls run in parallel under a small semaphore
              so the user doesn't wait minutes on a sequential round-trip per
              scenario, while staying within OpenRouter / provider rate limits.

Each phase-2 worker uses its own SQLAlchemy Session, so concurrent commits
(controls + ModelCall telemetry) don't race on a shared session. SQLite WAL
mode + `PRAGMA busy_timeout` (set in `app.db`) keeps writer contention safe.

Partial failures are durable: a worker that succeeds commits its controls
before another worker fails. Re-running `generate()` is idempotent (existing
description-sourced scenarios are deleted first), so the user can always
retry cleanly.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy.orm import Session

from app.ai.context import assessment_context_block
from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, OpenRouterError, call_structured
from app.db import SessionLocal
from app.models import Assessment, ExpectedControl, Scenario
from app.scoring.engine import band_for
from app.schemas.ai import (
    ExpectedControlListOut,
    ScenarioListOut,
    ScenarioOut,
    ScenarioSkeletonListOut,
    ScenarioSkeletonOut,
)

PHASE1_PROMPT = load_prompt("scenarios_phase1")
PHASE2_PROMPT = load_prompt("scenarios_phase2")
_CATALOG_PATH = Path(__file__).parent.parent / "catalog" / "controls.json"

ProgressCb = Callable[[float, str], Awaitable[None]]

# Bounded concurrency for phase-2 calls. 4 keeps us well within typical
# OpenRouter / Anthropic rate limits while giving a meaningful speedup.
MAX_PHASE2_CONCURRENCY = 4


def _catalog_lines() -> str:
    data = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return "\n".join(f"- {c['code']} — {c['name']} ({c['family']})" for c in data["controls"])


def _phase1_messages(
    description_summary: str, vendor_name: str, context: str = ""
) -> list[dict]:
    user_block = (
        f"{context}\n\n"
        f"# Vendor\n{vendor_name}\n\n"
        f"# Service description (final)\n{description_summary}\n\n"
        "Generate scenario skeletons per the schema."
    )
    return [
        {"role": "system", "content": PHASE1_PROMPT},
        {"role": "user", "content": user_block},
    ]


def _phase2_messages(
    description_summary: str,
    vendor_name: str,
    scenario: ScenarioSkeletonOut,
    context: str = "",
) -> list[dict]:
    user_block = (
        f"{context}\n\n"
        f"# Vendor\n{vendor_name}\n\n"
        f"# Service description\n{description_summary}\n\n"
        f"# Scenario\n"
        f"- code: {scenario.code}\n"
        f"- name: {scenario.name}\n"
        f"- description: {scenario.description}\n"
        f"- inherent_impact: {scenario.inherent_impact}\n"
        f"- inherent_likelihood: {scenario.inherent_likelihood}\n\n"
        f"# Control catalogue (use these codes when applicable)\n{_catalog_lines()}\n\n"
        "Return the expected controls for THIS scenario per the schema."
    )
    return [
        {"role": "system", "content": PHASE2_PROMPT},
        {"role": "user", "content": user_block},
    ]


async def _phase2_worker(
    *,
    scenario_id: int,
    sk: ScenarioSkeletonOut,
    description_summary: str,
    vendor_name: str,
    assessment_id: int,
    model_override: str | None,
    semaphore: asyncio.Semaphore,
    client: OpenRouterClient | None,
    on_done: Callable[[], Awaitable[None]] | None,
    context: str = "",
) -> ExpectedControlListOut:
    async with semaphore:
        # Each worker uses its own Session so concurrent commits (the controls
        # write and the ModelCall row written inside `call_structured`) don't
        # race with the parent session or with each other.
        with SessionLocal() as inner:
            controls_out: ExpectedControlListOut = await call_structured(
                inner,
                purpose="scenario_controls",
                profile="reasoner",
                messages=_phase2_messages(description_summary, vendor_name, sk, context),
                schema=ExpectedControlListOut,
                assessment_id=assessment_id,
                model_override=model_override,
                max_tokens=8192,  # dense scenarios overflowed 4096→8192; start at 8192 so the enlargement reaches 16k
                client=client,
            )
            for c_out in controls_out.expected_controls:
                inner.add(
                    ExpectedControl(
                        scenario_id=scenario_id,
                        code=c_out.code,
                        name=c_out.name,
                        description=c_out.description,
                        weight=c_out.weight,
                        rationale=c_out.rationale,
                    )
                )
            inner.commit()
    if on_done is not None:
        await on_done()
    return controls_out


async def generate(
    db: Session,
    assessment: Assessment,
    description_summary: str,
    *,
    on_progress: ProgressCb | None = None,
    client: OpenRouterClient | None = None,
) -> ScenarioListOut:
    model_override = (assessment.model_overrides or {}).get("scenarios")
    context = assessment_context_block(assessment)

    # ---- Phase 1: skeletons ----
    skeletons_out: ScenarioSkeletonListOut = await call_structured(
        db,
        purpose="scenario_skeletons",
        profile="reasoner",
        messages=_phase1_messages(description_summary, assessment.vendor_name, context),
        schema=ScenarioSkeletonListOut,
        assessment_id=assessment.id,
        model_override=model_override,
        max_tokens=4096,
        client=client,
    )
    n = len(skeletons_out.scenarios)
    if on_progress:
        await on_progress(0.2, f"Generated {n} scenario skeletons; selecting controls...")

    # Idempotent persistence: clear existing description-sourced scenarios.
    existing = (
        db.query(Scenario)
        .filter(Scenario.assessment_id == assessment.id, Scenario.source == "description")
        .all()
    )
    for s in existing:
        db.delete(s)
    db.flush()

    # Persist skeleton rows up-front so phase-2 workers (other sessions) can
    # write controls keyed by scenario_id, and so partial work survives.
    scenario_rows: list[tuple[int, ScenarioSkeletonOut]] = []
    for sk in skeletons_out.scenarios:
        s = Scenario(
            assessment_id=assessment.id,
            code=sk.code,
            name=sk.name,
            description=sk.description,
            source="description",
            inherent_impact=sk.inherent_impact,
            inherent_likelihood=sk.inherent_likelihood,
            residual_impact=sk.inherent_impact,
            residual_likelihood=sk.inherent_likelihood,
            # Inherent band from the 4x4 matrix; recalculate overwrites it
            # with the residual band once gap analysis has run.
            score_band=band_for(sk.inherent_impact, sk.inherent_likelihood),
        )
        db.add(s)
        db.flush()  # populate s.id
        scenario_rows.append((s.id, sk))
    db.commit()

    # ---- Phase 2: per-scenario expected controls (parallel, bounded) ----
    semaphore = asyncio.Semaphore(MAX_PHASE2_CONCURRENCY)
    done = 0

    async def report_done():
        nonlocal done
        done += 1
        if on_progress:
            await on_progress(
                0.2 + 0.8 * done / n,
                f"Generated controls for {done}/{n} scenarios",
            )

    tasks = [
        _phase2_worker(
            context=context,
            scenario_id=sid,
            sk=sk,
            description_summary=description_summary,
            vendor_name=assessment.vendor_name,
            assessment_id=assessment.id,
            model_override=model_override,
            semaphore=semaphore,
            client=client,
            on_done=report_done,
        )
        for (sid, sk) in scenario_rows
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    full_scenarios: list[ScenarioOut] = []
    failed: list[tuple[str, BaseException]] = []
    for (_, sk), result in zip(scenario_rows, results, strict=True):
        if isinstance(result, BaseException):
            failed.append((sk.code, result))
            continue
        full_scenarios.append(
            ScenarioOut(
                code=sk.code,
                name=sk.name,
                description=sk.description,
                inherent_impact=sk.inherent_impact,
                inherent_likelihood=sk.inherent_likelihood,
                expected_controls=list(result.expected_controls),
            )
        )

    if failed:
        first_code, first_err = failed[0]
        codes = ", ".join(c for c, _ in failed)
        msg = (
            f"{len(full_scenarios)}/{n} scenarios got controls; "
            f"{len(failed)} failed ({codes}). First failure on '{first_code}': {first_err}"
        )
        if isinstance(first_err, OpenRouterError):
            raise OpenRouterError(
                msg,
                transient=first_err.transient,
                upstream_code=first_err.upstream_code,
            )
        raise RuntimeError(msg)

    return ScenarioListOut(scenarios=full_scenarios)
