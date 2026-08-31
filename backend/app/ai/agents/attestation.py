"""Attestation profile extraction (fast profile) + deterministic checks (Phase 4).

`extract_profile` runs once per SOC / ISO / pen-test document (at upload,
after weakness extraction; re-runnable via the API). `apply_checks` runs the
pure checks for the whole assessment (in the cross-correlate step, when the
analysis date and standards are known) and persists the findings as
weaknesses / evidence notes with the profile quotes — replacing model-judged
staleness for these document kinds. Re-runs replace previous check rows
(never user-edited ones)."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy.orm import Session

from app.ai.context import analysis_date, standards_profile
from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, call_structured
from app.attestation_checks import CheckFinding, check_profile, check_required_attestations
from app.models import Assessment, Chunk, Document, Weakness
from app.schemas.attestation import AttestationProfileOut

ATTESTATION_KINDS = {"soc", "iso", "pentest"}
ORIGIN = "attestation_check"
SYSTEM_PROMPT = load_prompt("attestation_profile")


def _render(chunks: list[Chunk]) -> str:
    return "\n\n---\n\n".join(
        f"[chunk_id={c.id} section_path=\"{c.section_path}\"{f' page {c.page}' if c.page is not None else ''}]\n{c.text}"
        for c in chunks
    )


async def extract_profile(
    db: Session, document_id: int, *, client: OpenRouterClient | None = None
) -> AttestationProfileOut | None:
    doc = db.get(Document, document_id)
    if doc is None or doc.kind not in ATTESTATION_KINDS:
        return None
    chunks = sorted(doc.chunks, key=lambda c: c.ord)
    if not chunks:
        return None
    out: AttestationProfileOut = await call_structured(
        db,
        purpose="attestation_profile",
        profile="fast",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"# Document: {doc.filename} (kind: {doc.kind})\n\n{_render(chunks)}"},
        ],
        schema=AttestationProfileOut,
        assessment_id=doc.assessment_id,
        max_tokens=4096,
        client=client,
    )
    doc.attestation_profile = out.model_dump(exclude_none=True)
    db.commit()
    return out


def _dedupe_key(assessment_id: int, doc_id: int | None, code: str) -> str:
    return hashlib.sha256(f"{assessment_id}|attestation|{doc_id or 0}|{code}".encode()).hexdigest()[:120]


def _bind_chunk(db: Session, doc_id: int | None, quotes: list[str]) -> tuple[int | None, list[dict]]:
    """Bind each quote to the chunk that contains it (same matcher as gap
    analysis). Unlocatable quotes stay as refs without a chunk_id."""
    from app.ai.agents.gap_analysis import _quote_in_chunk

    refs: list[dict] = []
    first_chunk = None
    chunks = (
        db.query(Chunk).filter(Chunk.document_id == doc_id).order_by(Chunk.ord).all()
        if doc_id
        else []
    )
    for q in quotes:
        cid = next((c.id for c in chunks if _quote_in_chunk(q, c.text)), None)
        c = next((c for c in chunks if c.id == cid), None)
        refs.append({
            "document_id": doc_id, "chunk_id": cid,
            "page": c.page if c else None, "section_path": c.section_path if c else "",
            "quote": q,
        })
        if first_chunk is None and cid is not None:
            first_chunk = cid
    return first_chunk, refs


def apply_checks(db: Session, assessment_id: int) -> dict[str, int]:
    """Run the deterministic checks and persist findings. Idempotent: this
    origin's non-user rows are replaced on every run."""
    a = db.get(Assessment, assessment_id)
    if a is None:
        raise ValueError(f"Assessment {assessment_id} not found")
    as_of = analysis_date(a)
    standards = standards_profile(a)

    (
        db.query(Weakness)
        .filter(
            Weakness.assessment_id == assessment_id,
            Weakness.origin == ORIGIN,
            Weakness.user_edited.is_(False),
        )
        .delete(synchronize_session=False)
    )

    profiles: dict[str, AttestationProfileOut] = {}
    findings: list[tuple[Document | None, CheckFinding]] = []
    for doc in a.documents:
        if not doc.attestation_profile:
            continue
        profile = AttestationProfileOut.model_validate(doc.attestation_profile)
        profiles[doc.filename] = profile
        for f in check_profile(profile, as_of, standards, doc_label=doc.filename):
            findings.append((doc, f))
    for f in check_required_attestations(profiles, standards):
        findings.append((None, f))

    counts = {"weakness": 0, "evidence_note": 0}
    now = datetime.utcnow().isoformat() + "Z"
    for doc, f in findings:
        doc_id = doc.id if doc is not None else None
        chunk_id, refs = _bind_chunk(db, doc_id, f.quotes)
        counts[f.kind] += 1
        db.add(Weakness(
            assessment_id=assessment_id,
            source_document_id=doc_id,
            source_chunk_id=chunk_id,
            severity=f.severity,
            description=f.description,
            mapped_control_codes=list(f.suggested_control_codes),
            quote=f.quotes[0] if f.quotes else "",
            unmatched=True,
            kind_signal="attestation_check",
            dedupe_key=_dedupe_key(assessment_id, doc_id, f.code),
            origin=ORIGIN,
            evidence_refs=refs,
            status="confirmed" if f.kind == "weakness" else "evidence_note",
            review={
                "decision": "confirmed" if f.kind == "weakness" else "evidence_note",
                "reason": f"deterministic attestation check '{f.code}' against analysis date {as_of} and the assessor standards",
                "confidence": "high",
                "at": now,
                "stage": "attestation_check",
            },
        ))
    db.commit()
    return counts
