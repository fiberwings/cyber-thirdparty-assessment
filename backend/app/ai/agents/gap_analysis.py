"""Per-control gap analysis — for every expected control on every scenario,
retrieve candidate evidence, ask the reasoner to assess, persist."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai import retrieval
from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, call_structured
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
        max_tokens=1024,
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


async def run_full(
    db: Session,
    assessment: Assessment,
    *,
    client: OpenRouterClient | None = None,
    on_progress=None,
):
    """Iterate every scenario × control and run gap analysis."""
    scenarios = list(assessment.scenarios)
    total = sum(len(s.expected_controls) for s in scenarios)
    done = 0
    for s in scenarios:
        for ctrl in s.expected_controls:
            await assess_control(db, assessment, s, ctrl, client=client)
            done += 1
            if on_progress:
                on_progress(done, total, f"{s.code}/{ctrl.code}")
