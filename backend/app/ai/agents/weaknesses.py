"""Weakness synthesizer + emergent scenarios.

We feed the reasoner a windowed pool of "interesting" chunks (pen test +
questionnaire negatives + documents tagged as such) plus the current scenario
codes, and ask it to surface concrete weaknesses and propose emergent scenarios.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, call_structured
from app.models import (
    Assessment,
    Chunk,
    Document,
    ExpectedControl,
    Scenario,
    Weakness,
)
from app.schemas.ai import WeaknessSynthesisOut

SYSTEM_PROMPT = load_prompt("weaknesses")

_NEGATIVE_TERMS = (
    "no",
    "not",
    "none",
    "lack",
    "missing",
    "absence",
    "exception",
    "deficien",
    "high",
    "critical",
    "open",
    "unremediated",
    "unresolved",
    "gap",
    "weakness",
    "noncompliant",
    "non-compliant",
    "findings",
    "vulnerab",
)


def _interesting_chunks(db: Session, assessment_id: int, max_chunks: int = 60) -> list[Chunk]:
    """Pull a mixed pool: all pentest doc chunks first, then questionnaire/SOC chunks
    that appear to contain negative-signal words.
    """
    pentest_chunks: list[Chunk] = (
        db.query(Chunk)
        .join(Document, Document.id == Chunk.document_id)
        .filter(Document.assessment_id == assessment_id, Document.kind == "pentest")
        .order_by(Document.id, Chunk.ord)
        .limit(max_chunks // 2)
        .all()
    )
    remaining = max_chunks - len(pentest_chunks)
    other_chunks: list[Chunk] = []
    if remaining > 0:
        candidates = (
            db.query(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .filter(
                Document.assessment_id == assessment_id,
                Document.kind != "pentest",
            )
            .order_by(Document.id, Chunk.ord)
            .all()
        )
        for c in candidates:
            low = c.text.lower()
            if any(t in low for t in _NEGATIVE_TERMS):
                other_chunks.append(c)
                if len(other_chunks) >= remaining:
                    break
    return pentest_chunks + other_chunks


def _format_chunks(chunks: list[Chunk]) -> str:
    parts = []
    for c in chunks:
        loc_bits = []
        if c.page is not None:
            loc_bits.append(f"page {c.page}")
        if c.section_path:
            loc_bits.append(c.section_path)
        loc = " — ".join(loc_bits) if loc_bits else "section unknown"
        parts.append(f"[document_id={c.document_id} chunk_id={c.id} {loc}]\n{c.text}")
    return "\n\n---\n\n".join(parts) if parts else "(no evidence)"


def _build_messages(
    description_summary: str,
    existing_codes: list[str],
    chunks: list[Chunk],
) -> list[dict]:
    user_block = (
        f"# Service description summary\n{description_summary}\n\n"
        f"# Existing scenario codes (do not re-emit)\n"
        + ("\n".join(f"- {c}" for c in existing_codes) or "(none)")
        + f"\n\n# Evidence pool\n{_format_chunks(chunks)}\n\n"
        "Identify weaknesses and emergent scenarios per the schema."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


async def synthesize(
    db: Session,
    assessment: Assessment,
    description_summary: str,
    *,
    client: OpenRouterClient | None = None,
) -> WeaknessSynthesisOut:
    chunks = _interesting_chunks(db, assessment.id)
    chunk_lookup = {c.id: c for c in chunks}
    existing_codes = [s.code for s in assessment.scenarios]

    out: WeaknessSynthesisOut = await call_structured(
        db,
        purpose="weakness_synthesis",
        profile="reasoner",
        messages=_build_messages(description_summary, existing_codes, chunks),
        schema=WeaknessSynthesisOut,
        assessment_id=assessment.id,
        model_override=(assessment.model_overrides or {}).get("weaknesses"),
        max_tokens=4096,
        client=client,
    )

    # Persist weaknesses
    for w_out in out.weaknesses:
        chunk_id = None
        if w_out.citation:
            chunk_id = w_out.citation.chunk_id
            if chunk_id is None:
                for cid, c in chunk_lookup.items():
                    if c.document_id == w_out.citation.document_id and (
                        not w_out.citation.quote
                        or w_out.citation.quote[:30].lower() in c.text.lower()
                    ):
                        chunk_id = cid
                        break
        db.add(
            Weakness(
                assessment_id=assessment.id,
                source_chunk_id=chunk_id,
                severity=w_out.severity,
                description=w_out.description,
                mapped_control_codes=w_out.mapped_control_codes,
                quote=w_out.quote or (w_out.citation.quote if w_out.citation else ""),
            )
        )

    # Persist emergent scenarios that have a code we haven't seen
    seen = set(existing_codes)
    for s_out in out.emergent_scenarios:
        if s_out.code in seen:
            continue
        s = Scenario(
            assessment_id=assessment.id,
            code=s_out.code,
            name=s_out.name,
            description=s_out.description,
            source="emergent",
            inherent_impact=s_out.inherent_impact,
            inherent_likelihood=s_out.inherent_likelihood,
            residual_impact=s_out.inherent_impact,
            residual_likelihood=s_out.inherent_likelihood,
            score_band="Moderate",
        )
        db.add(s)
        db.flush()
        for c_out in s_out.expected_controls:
            db.add(
                ExpectedControl(
                    scenario_id=s.id,
                    code=c_out.code,
                    name=c_out.name,
                    description=c_out.description,
                    weight=c_out.weight,
                    rationale=c_out.rationale,
                )
            )
        seen.add(s_out.code)

    db.commit()
    return out
