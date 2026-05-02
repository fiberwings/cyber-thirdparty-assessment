"""Per-control gap analysis — for every expected control on every scenario,
retrieve candidate evidence, ask the reasoner to assess, persist.

`run_full` fans the per-control work out across an `asyncio.Semaphore` so that
several reasoner calls are in flight at once. Each worker uses its own
SQLAlchemy Session (matching the pattern in `scenarios.py` phase-2): SQLite
WAL + `PRAGMA busy_timeout` keeps writer contention safe, and partial work is
durable — a control that succeeds commits before another fails.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from app.ai import retrieval
from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, OpenRouterError, call_structured
from app.db import SessionLocal
from app.models import (
    Assessment,
    Chunk,
    ControlAssessment,
    ControlEvidence,
    ExpectedControl,
    MetaIssue,
    Scenario,
)
from app.schemas.ai import ControlAssessmentOut

SYSTEM_PROMPT = load_prompt("gap_analysis")

# Bounded concurrency for per-control gap-analysis calls. Mirrors the
# phase-2 scenario generator — 4 keeps us comfortably inside typical
# OpenRouter / Anthropic rate limits while giving a meaningful speedup
# on assessments with dozens of controls.
MAX_GAP_ANALYSIS_CONCURRENCY = 4


def _format_chunks(chunks: list[Chunk]) -> str:
    parts = []
    for c in chunks:
        loc_bits = []
        if c.page is not None:
            loc_bits.append(f"page {c.page}")
        if c.section_path:
            loc_bits.append(c.section_path)
        loc = " — ".join(loc_bits) if loc_bits else "section unknown"
        parts.append(
            f"[document_id={c.document_id} chunk_id={c.id} {loc}]\n{c.text}"
        )
    return "\n\n---\n\n".join(parts) if parts else "(no candidate evidence found)"


def _build_messages(scenario: Scenario, control: ExpectedControl, chunks: list[Chunk]) -> list[dict]:
    user_block = (
        f"# Scenario\n{scenario.code} — {scenario.name}\n{scenario.description}\n\n"
        f"# Control under assessment\n"
        f"code: {control.code}\nname: {control.name}\n"
        f"description: {control.description}\nrationale: {control.rationale}\n\n"
        f"# Candidate evidence\n{_format_chunks(chunks)}\n\n"
        "Assess this control. Output JSON per schema."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


def _retrieve_for_control(
    db: Session, assessment_id: int, control: ExpectedControl, scenario: Scenario
) -> list[Chunk]:
    return retrieval.search(
        db,
        assessment_id,
        control.code.replace(".", " "),
        control.name,
        control.description or "",
        scenario.name,
    )


async def assess_control(
    db: Session,
    assessment: Assessment,
    scenario: Scenario,
    control: ExpectedControl,
    *,
    client: OpenRouterClient | None = None,
) -> ControlAssessmentOut:
    chunks = _retrieve_for_control(db, assessment.id, control, scenario)
    chunk_index = {c.id: c for c in chunks}

    out: ControlAssessmentOut = await call_structured(
        db,
        purpose="gap_analysis_control",
        profile="reasoner",
        messages=_build_messages(scenario, control, chunks),
        schema=ControlAssessmentOut,
        assessment_id=assessment.id,
        model_override=(assessment.model_overrides or {}).get("gap_analysis"),
        max_tokens=4096,
        client=client,
    )

    ca = control.assessment or ControlAssessment(expected_control_id=control.id)
    if ca.is_locked_by_user:
        return out  # respect user edits
    ca.coverage = out.coverage
    ca.effectiveness = out.effectiveness
    ca.rationale = out.rationale
    if not ca.id:
        db.add(ca)
        db.flush()

    # Replace evidence rows
    for ev in list(ca.evidence):
        db.delete(ev)
    db.flush()
    for cite in out.citations:
        chunk_id = cite.chunk_id
        if chunk_id is None and cite.document_id and cite.quote:
            # Find any chunk whose text contains the quote — best-effort
            for cid, c in chunk_index.items():
                if c.document_id == cite.document_id and cite.quote[:30].lower() in c.text.lower():
                    chunk_id = cid
                    break
        if chunk_id is None:
            # As a last resort, attach to the first chunk from that document we retrieved
            for cid, c in chunk_index.items():
                if c.document_id == cite.document_id:
                    chunk_id = cid
                    break
        if chunk_id is None:
            continue
        db.add(
            ControlEvidence(
                control_assessment_id=ca.id,
                chunk_id=chunk_id,
                polarity="supports",
                quote=cite.quote,
                ai_rationale=out.rationale[:500],
            )
        )

    # Meta flags become MetaIssues at the assessment level
    for flag in out.meta_flags:
        db.add(
            MetaIssue(
                assessment_id=assessment.id,
                kind=flag,
                target_ref=f"{scenario.code}/{control.code}",
                weight={"insufficient_info": 1.0, "vague_answer": 0.5,
                        "missing_doc": 0.75, "conflicting_evidence": 1.0}.get(flag, 0.5),
                rationale=out.rationale[:300],
                scenario_code=scenario.code,
            )
        )

    db.commit()
    return out


async def _assess_control_worker(
    *,
    assessment_id: int,
    scenario_id: int,
    control_id: int,
    semaphore: asyncio.Semaphore,
    client: OpenRouterClient | None,
    on_done,
    label: str,
) -> None:
    async with semaphore:
        # Each worker uses its own Session so concurrent commits (control
        # assessment + evidence + ModelCall telemetry) don't race on a
        # shared session.
        with SessionLocal() as inner:
            assessment = inner.get(Assessment, assessment_id)
            scenario = inner.get(Scenario, scenario_id)
            control = inner.get(ExpectedControl, control_id)
            await assess_control(inner, assessment, scenario, control, client=client)
    if on_done is not None:
        on_done(label)


async def run_full(
    db: Session,
    assessment: Assessment,
    *,
    client: OpenRouterClient | None = None,
    on_progress=None,
):
    """Iterate every scenario × control and run gap analysis (parallel, bounded)."""
    # Snapshot ids from the parent session before fanning out — workers will
    # re-load the rows in their own sessions.
    targets: list[tuple[int, int, str]] = []
    for s in assessment.scenarios:
        for ctrl in s.expected_controls:
            targets.append((s.id, ctrl.id, f"{s.code}/{ctrl.code}"))
    total = len(targets)
    if total == 0:
        return

    semaphore = asyncio.Semaphore(MAX_GAP_ANALYSIS_CONCURRENCY)
    done = 0

    def report_done(label: str) -> None:
        nonlocal done
        done += 1
        if on_progress:
            on_progress(done, total, label)

    tasks = [
        _assess_control_worker(
            assessment_id=assessment.id,
            scenario_id=sid,
            control_id=cid,
            semaphore=semaphore,
            client=client,
            on_done=report_done,
            label=label,
        )
        for (sid, cid, label) in targets
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    failed: list[tuple[str, BaseException]] = [
        (label, r) for (_, _, label), r in zip(targets, results, strict=True)
        if isinstance(r, BaseException)
    ]
    if failed:
        first_label, first_err = failed[0]
        labels = ", ".join(lbl for lbl, _ in failed)
        msg = (
            f"{total - len(failed)}/{total} controls assessed; "
            f"{len(failed)} failed ({labels}). First failure on '{first_label}': {first_err}"
        )
        if isinstance(first_err, OpenRouterError):
            raise OpenRouterError(
                msg,
                transient=first_err.transient,
                upstream_code=first_err.upstream_code,
            )
        raise RuntimeError(msg)
