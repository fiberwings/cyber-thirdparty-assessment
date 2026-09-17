"""Gap analysis.

Whole-bundle mode (R1, default when the parsed bundle fits the reasoner):
every distinct expected-control *code* is assessed ONCE per assessment
against the complete evidence bundle, in batches of a few related codes per
reasoner call. The verdict is written to every scenario that expects the
code (so verdicts are consistent by construction) and contradictions between
any documents are detected in the same whole-context call.

Per-control retrieval mode (fallback for oversized bundles): for every
expected control on every scenario, retrieve candidate evidence with FTS,
ask the reasoner to assess, persist — with one bounded second retrieval pass.

`run_full` fans the per-control work out across an `asyncio.Semaphore` so that
several reasoner calls are in flight at once. Each worker uses its own
SQLAlchemy Session (matching the pattern in `scenarios.py` phase-2): SQLite
WAL + `PRAGMA busy_timeout` keeps writer contention safe, and partial work is
durable — a control that succeeds commits before another fails.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai import retrieval
from app.ai.context import assessment_context_block
from app.ai.prompts import load as load_prompt
from app.ai.router import LLMClient, LLMError, call_structured
from app.config import settings
from app.db import SessionLocal
from app.models import (
    Assessment,
    Chunk,
    ControlAssessment,
    ControlEvidence,
    ExpectedControl,
    MetaIssue,
    Scenario,
    Weakness,
)
from app.models import Document
from app.schemas.ai import CitationOut, ControlAssessmentOut, ControlBatchOut
from app.scoring.engine import _META_WEIGHTS

CONTRADICTION_KIND = "cross_doc_conflict"
GAP_ANALYSIS_ORIGIN = "gap_analysis"
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

SYSTEM_PROMPT = load_prompt("gap_analysis")
BUNDLE_SYSTEM_PROMPT = load_prompt("gap_analysis_bundle")

# Whole-bundle mode is used when the parsed bundle (all chunks) is at most
# this many approximate tokens (len/4). Above it, the per-control retrieval
# path runs instead. Every benchmark vendor is 15–25k tokens.
WHOLE_BUNDLE_MAX_TOKENS = 100_000
# Distinct control codes per whole-bundle call. Related families are packed
# together; a batch never splits a family unless the family is larger.
BUNDLE_BATCH_SIZE = 5
# Whole-bundle output budget comes from settings.llm_budget_large (the batch
# covers BUNDLE_BATCH_SIZE controls, so it needs the large synthesis tier).
# Whole-bundle calls run with lower parallelism than the per-control path so
# that most batches see the contradictions already reported by finished
# batches (cross-batch duplicate suppression is done by the model, in
# context, not by a rule).
BUNDLE_CONCURRENCY = 2

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


def _known_weaknesses_for_control(
    db: Session, assessment_id: int, control_code: str
) -> list[Weakness]:
    """Document-extracted weaknesses cross-correlation already mapped onto
    this control code. Shown to the model so its verdict accounts for them
    and so it does not re-emit them as contradictions."""
    rows = (
        db.query(Weakness)
        .filter(
            Weakness.assessment_id == assessment_id,
            Weakness.origin != GAP_ANALYSIS_ORIGIN,
            Weakness.status == "confirmed",
        )
        .order_by(Weakness.id)
        .all()
    )
    return [w for w in rows if control_code in (w.mapped_control_codes or [])]


def _format_known_weaknesses(weaknesses: list[Weakness]) -> str:
    if not weaknesses:
        return "(none)"
    parts = []
    for w in weaknesses:
        doc = w.document.filename if w.document is not None else "unknown document"
        quote = f'\n  quote: "{w.quote}"' if w.quote else ""
        parts.append(
            f"- [weakness_id={w.id} severity={w.severity} source={doc}] "
            f"{w.description}{quote}"
        )
    return "\n".join(parts)


def _build_messages(
    scenario: Scenario,
    control: ExpectedControl,
    chunks: list[Chunk],
    *,
    known_weaknesses: list[Weakness] | None = None,
    second_pass: bool = False,
    context: str = "",
) -> list[dict]:
    note = (
        "This is the SECOND retrieval pass — the candidate evidence now includes "
        "the extra chunks located with your proposed queries. This is your final "
        "assessment: judge on the combined evidence and return proposed_queries: [].\n\n"
        if second_pass
        else ""
    )
    user_block = (
        f"{note}"
        f"{context}\n\n"
        f"# Scenario\n{scenario.code} — {scenario.name}\n{scenario.description}\n\n"
        f"# Control under assessment\n"
        f"code: {control.code}\nname: {control.name}\n"
        f"description: {control.description}\nrationale: {control.rationale}\n\n"
        f"# Known weaknesses already mapped to this control\n"
        f"{_format_known_weaknesses(known_weaknesses or [])}\n\n"
        f"# Candidate evidence\n{_format_chunks(chunks)}\n\n"
        "Assess this control. Output JSON per schema."
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


_NON_WORD = re.compile(r"[^0-9a-z]+")


def _norm(s: str) -> str:
    """Lower-case, punctuation-free, single-spaced. Chunk text carries
    markdown markers (**bold**), table line breaks and typographic dashes
    that a verbatim quote legitimately lacks; comparing word sequences keeps
    the match strict (same words, same order, contiguous) while ignoring
    those artefacts."""
    return " ".join(_NON_WORD.split((s or "").lower())).strip()


def _quote_in_chunk(quote: str, chunk_text: str) -> bool:
    """True when the (normalized) quote genuinely appears in the chunk.

    Falls back to a long prefix for quotes that straddle a chunk boundary —
    but never to anything shorter, so a match is always a real occurrence.
    """
    q = _norm(quote)
    if len(q) < 10:
        return False  # too short to be a trustworthy anchor
    t = _norm(chunk_text)
    if q in t or (len(q) > 60 and q[:60] in t):
        return True
    # Abbreviated quote ("A ... B ... C"): every fragment must occur, in order.
    # Each fragment is a real occurrence, so this is still a genuine match.
    frags = [f.strip() for f in re.split(r"\.\.\.|…", q) if f.strip()]
    if len(frags) < 2 or any(len(f) < 10 for f in frags):
        return False
    pos = 0
    for f in frags:
        i = t.find(f, pos)
        if i < 0:
            return False
        pos = i + len(f)
    return True


def _resolve_citation_chunk(
    db: Session,
    cite: CitationOut,
    chunk_index: dict[int, Chunk],
    doc_chunk_cache: dict[int, list[Chunk]],
) -> int | None:
    """Find the chunk whose text actually contains the cited quote.

    Order: the model's chunk_id (only if the quote is in it) → retrieved
    chunks of the cited document → every chunk of that document. Returns None
    when the quote cannot be located anywhere — evidence is never bound to a
    chunk it does not appear in.
    """
    if cite.chunk_id is not None:
        c = chunk_index.get(cite.chunk_id)
        if c is not None and _quote_in_chunk(cite.quote, c.text):
            return cite.chunk_id
    if not cite.document_id:
        return None
    for cid, c in chunk_index.items():
        if c.document_id == cite.document_id and _quote_in_chunk(cite.quote, c.text):
            return cid
    if cite.document_id not in doc_chunk_cache:
        doc_chunk_cache[cite.document_id] = (
            db.query(Chunk)
            .filter(Chunk.document_id == cite.document_id)
            .order_by(Chunk.ord)
            .all()
        )
    for c in doc_chunk_cache[cite.document_id]:
        if c.id not in chunk_index and _quote_in_chunk(cite.quote, c.text):
            return c.id
    return None


def _contradiction_dedupe_key(assessment_id: int, quotes: list[str]) -> str:
    """Assessment-level identity of a contradiction: the set of quotes that
    disagree, independent of which control surfaced it — so the same
    disagreement seen under two controls collapses into one row."""
    payload = f"{assessment_id}|contradiction|" + "|".join(
        sorted(_norm(q)[:200] for q in quotes)
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:120]


def _restates_known_weakness(
    claims: list[tuple[CitationOut, int | None]], known: list[Weakness]
) -> bool:
    """Deterministic backstop for the prompt rule "do not report a
    contradiction between a known weakness and a claim it already refutes".

    If ANY side of the contradiction is already the evidence of a weakness
    mapped to this control (same chunk, or one quote contains the other),
    the contradiction is that weakness restated against the vendor's claim —
    the control failure is already scored once, so it must not score twice.
    """
    if not known:
        return False
    known_chunks = {w.source_chunk_id for w in known if w.source_chunk_id}
    known_quotes = [_norm(w.quote) for w in known if w.quote and len(_norm(w.quote)) >= 10]
    for cite, chunk_id in claims:
        if chunk_id is not None and chunk_id in known_chunks:
            return True
        nq = _norm(cite.quote)
        if len(nq) < 10:
            continue
        if any(nq in kq or kq in nq for kq in known_quotes):
            return True
    return False


def _release_contradiction_claims(
    db: Session, assessment_id: int, target_ref: str, control_code: str
) -> None:
    """Idempotency for re-runs: drop this target's claim on every
    gap-analysis weakness it previously raised. Rows nobody claims any more
    are deleted (unless a user edited them), mirroring how this control's
    MetaIssues are replaced on each run."""
    rows = (
        db.query(Weakness)
        .filter(
            Weakness.assessment_id == assessment_id,
            Weakness.origin == GAP_ANALYSIS_ORIGIN,
        )
        .all()
    )
    for w in rows:
        refs = list(w.origin_refs or [])
        if target_ref not in refs:
            continue
        refs.remove(target_ref)
        w.origin_refs = refs
        if refs:
            # Other controls still claim it; only drop this control's code if
            # no remaining claimant is on the same control code.
            still_on_code = any(r.split("/", 1)[-1] == control_code for r in refs)
            if not still_on_code:
                w.mapped_control_codes = [
                    c for c in (w.mapped_control_codes or []) if c != control_code
                ]
        elif not w.user_edited:
            db.delete(w)
        else:
            # Keep the user's row but stop scoring it through this control.
            w.mapped_control_codes = [
                c for c in (w.mapped_control_codes or []) if c != control_code
            ]
            w.unmatched = not w.mapped_control_codes
    db.flush()


def _persist_contradictions(
    db: Session,
    *,
    assessment_id: int,
    scenario: Scenario,
    control: ExpectedControl,
    ca: ControlAssessment,
    out: ControlAssessmentOut,
    chunk_index: dict[int, Chunk],
    doc_chunk_cache: dict[int, list[Chunk]],
    known: list[Weakness],
    unresolved: list[dict],
) -> None:
    """Turn each reported contradiction into exactly one scored Weakness.

    A contradiction is a vendor finding, so it takes the weakness scoring
    path (severity uplift via `mapped_control_codes`) and never also becomes
    a MetaIssue. Identity is the pair of disagreeing quotes: a second control
    that sees the same pair appends its code instead of adding a row.
    """
    target_ref = f"{scenario.code}/{control.code}"
    for con in out.contradictions:
        quotes = [c.quote for c in con.claims]
        resolved = [
            (claim, _resolve_citation_chunk(db, claim, chunk_index, doc_chunk_cache))
            for claim in con.claims
        ]
        if _restates_known_weakness(resolved, known):
            continue
        refs: list[dict] = []
        for claim, chunk_id in resolved:
            ref = {
                "document_id": claim.document_id,
                "chunk_id": chunk_id,
                "page": claim.page,
                "section_path": claim.section_path,
                "quote": claim.quote,
            }
            refs.append(ref)
            if chunk_id is None:
                unresolved.append({k: ref[k] for k in ("document_id", "page", "section_path", "quote")})
            else:
                db.add(
                    ControlEvidence(
                        control_assessment_id=ca.id,
                        chunk_id=chunk_id,
                        polarity="contradicts",
                        quote=claim.quote,
                        ai_rationale=con.description[:500],
                    )
                )
        key = _contradiction_dedupe_key(assessment_id, quotes)
        existing = (
            db.query(Weakness)
            .filter(Weakness.assessment_id == assessment_id, Weakness.dedupe_key == key)
            .one_or_none()
        )
        if existing is not None:
            existing.mapped_control_codes = sorted(
                set(existing.mapped_control_codes or []) | {control.code}
            )
            existing.unmatched = False
            if target_ref not in (existing.origin_refs or []):
                existing.origin_refs = [*(existing.origin_refs or []), target_ref]
            if _SEVERITY_RANK.get(con.severity, 0) > _SEVERITY_RANK.get(existing.severity, 0):
                existing.severity = con.severity
            if not existing.user_edited:
                existing.description = con.description
            continue
        first = refs[0]
        row = Weakness(
            assessment_id=assessment_id,
            source_chunk_id=first["chunk_id"],
            source_document_id=first["document_id"] or None,
            severity=con.severity,
            description=con.description,
            mapped_control_codes=[control.code],
            quote=first["quote"],
            unmatched=False,
            kind_signal=CONTRADICTION_KIND,
            dedupe_key=key,
            origin=GAP_ANALYSIS_ORIGIN,
            evidence_refs=refs,
            origin_refs=[target_ref],
        )
        try:
            # Parallel workers (separate sessions) can discover the same
            # contradiction at once; the unique (assessment_id, dedupe_key)
            # index makes exactly one insert win. Use a savepoint so the loser
            # keeps the rest of its control's work and merges instead.
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            db.expire_all()
            winner = (
                db.query(Weakness)
                .filter(Weakness.assessment_id == assessment_id, Weakness.dedupe_key == key)
                .one()
            )
            winner.mapped_control_codes = sorted(
                set(winner.mapped_control_codes or []) | {control.code}
            )
            winner.unmatched = False
            if target_ref not in (winner.origin_refs or []):
                winner.origin_refs = [*(winner.origin_refs or []), target_ref]
    db.flush()


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
    client: LLMClient | None = None,
) -> ControlAssessmentOut:
    chunks = _retrieve_for_control(db, assessment.id, control, scenario)
    known = _known_weaknesses_for_control(db, assessment.id, control.code)
    model_override = (assessment.model_overrides or {}).get("gap_analysis")
    context = assessment_context_block(assessment)

    out: ControlAssessmentOut = await call_structured(
        db,
        purpose="gap_analysis_control",
        profile="reasoner",
        messages=_build_messages(
            scenario, control, chunks, known_weaknesses=known, context=context
        ),
        schema=ControlAssessmentOut,
        assessment_id=assessment.id,
        model_override=model_override,
        max_tokens=settings.llm_budget_medium,
        client=client,
    )

    # Agentic second retrieval pass (bounded to exactly one extra round): when
    # the first pass found no usable evidence and the model proposed better
    # search terms, re-retrieve and re-assess on the combined evidence.
    needs_more = out.coverage == "none" or "insufficient_info" in out.meta_flags
    if needs_more and out.proposed_queries:
        extra = retrieval.search(db, assessment.id, *out.proposed_queries)
        seen_ids = {c.id for c in chunks}
        new_chunks = [c for c in extra if c.id not in seen_ids]
        if new_chunks:
            chunks = chunks + new_chunks
            out = await call_structured(
                db,
                purpose="gap_analysis_control_r2",
                profile="reasoner",
                messages=_build_messages(
                    scenario, control, chunks, known_weaknesses=known,
                    second_pass=True, context=context,
                ),
                schema=ControlAssessmentOut,
                assessment_id=assessment.id,
                model_override=model_override,
                max_tokens=settings.llm_budget_medium,
                client=client,
            )

    _persist_control_output(
        db, assessment, scenario, control, out, {c.id: c for c in chunks}, known
    )
    return out


def _persist_control_output(
    db: Session,
    assessment: Assessment,
    scenario: Scenario,
    control: ExpectedControl,
    out: ControlAssessmentOut,
    chunk_index: dict[int, Chunk],
    known: list[Weakness],
) -> None:
    """Write one verdict to one expected control: assessment row, evidence
    (citations bound only to chunks that contain the quote), contradictions
    as scored weaknesses, meta-flags as meta-issues. Commits."""
    ca = control.assessment or ControlAssessment(expected_control_id=control.id)
    if ca.is_locked_by_user:
        return  # respect user edits
    ca.coverage = out.coverage
    ca.effectiveness = out.effectiveness
    ca.rationale = out.rationale
    ca.last_error = None
    ca.last_run_at = datetime.utcnow()
    if not ca.id:
        db.add(ca)
        db.flush()

    # Replace evidence rows
    for ev in list(ca.evidence):
        db.delete(ev)
    db.flush()
    doc_chunk_cache: dict[int, list[Chunk]] = {}
    unresolved: list[dict] = []
    for cite in out.citations:
        chunk_id = _resolve_citation_chunk(db, cite, chunk_index, doc_chunk_cache)
        if chunk_id is None:
            # Never bind evidence to a chunk the quote does not appear in.
            # Keep the citation verbatim as unresolved — itself a signal about
            # evidence quality.
            unresolved.append(
                {
                    "document_id": cite.document_id,
                    "page": cite.page,
                    "section_path": cite.section_path,
                    "quote": cite.quote,
                }
            )
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

    # Contradictions between sources are vendor findings: they become scored
    # weaknesses (one row per disagreement, shared across controls), never
    # meta-issues. Release this control's previous claims first so re-runs
    # replace rather than stack.
    target_ref = f"{scenario.code}/{control.code}"
    _release_contradiction_claims(db, assessment.id, target_ref, control.code)
    _persist_contradictions(
        db,
        assessment_id=assessment.id,
        scenario=scenario,
        control=control,
        ca=ca,
        out=out,
        chunk_index=chunk_index,
        doc_chunk_cache=doc_chunk_cache,
        known=known,
        unresolved=unresolved,
    )
    ca.unresolved_citations = unresolved

    # Meta flags become MetaIssues at the assessment level. Clear this
    # control's previous flags first so re-running gap analysis stays
    # idempotent instead of stacking duplicate uplift.
    (
        db.query(MetaIssue)
        .filter(
            MetaIssue.assessment_id == assessment.id,
            MetaIssue.target_ref == target_ref,
            MetaIssue.kind != "unscored_finding",
        )
        .delete(synchronize_session=False)
    )
    for flag in out.meta_flags:
        db.add(
            MetaIssue(
                assessment_id=assessment.id,
                kind=flag,
                target_ref=target_ref,
                weight=_META_WEIGHTS.get(flag, 0.5),
                rationale=out.rationale[:300],
                scenario_code=scenario.code,
            )
        )

    db.commit()


# ---------------- whole-bundle mode (R1) ----------------


def _approx_tokens(text: str) -> int:
    return len(text) // 4


def load_bundle(db: Session, assessment_id: int) -> tuple[list[Document], list[Chunk]]:
    docs = (
        db.query(Document)
        .filter(Document.assessment_id == assessment_id)
        .order_by(Document.id)
        .all()
    )
    chunks = (
        db.query(Chunk)
        .join(Document, Document.id == Chunk.document_id)
        .filter(Document.assessment_id == assessment_id)
        .order_by(Chunk.document_id, Chunk.ord)
        .all()
    )
    return docs, chunks


def format_bundle(docs: list[Document], chunks: list[Chunk]) -> str:
    by_doc: dict[int, list[Chunk]] = {}
    for c in chunks:
        by_doc.setdefault(c.document_id, []).append(c)
    parts = [f"# Evidence bundle — {len(docs)} document(s)"]
    parts.append(
        "\n".join(f"- document_id={d.id} kind={d.kind} file=\"{d.filename}\"" for d in docs)
        or "(no documents)"
    )
    for d in docs:
        parts.append(f"\n## document_id={d.id} kind={d.kind} — {d.filename}")
        parts.append(_format_chunks(by_doc.get(d.id, [])))
    return "\n".join(parts)


def bundle_fits(chunks: list[Chunk]) -> bool:
    return _approx_tokens("".join(c.text for c in chunks)) <= WHOLE_BUNDLE_MAX_TOKENS


def group_controls_by_code(assessment: Assessment) -> dict[str, list[tuple[Scenario, ExpectedControl]]]:
    groups: dict[str, list[tuple[Scenario, ExpectedControl]]] = {}
    for s in assessment.scenarios:
        for ctrl in s.expected_controls:
            groups.setdefault(ctrl.code, []).append((s, ctrl))
    return groups


def _family(code: str) -> str:
    return code.split(".", 1)[0] if "." in code else code


def batch_codes(codes: list[str], size: int = BUNDLE_BATCH_SIZE) -> list[list[str]]:
    """Pack codes into batches of ≤ size, keeping a family together when it
    fits in the remaining room (families larger than a batch are split)."""
    fams: dict[str, list[str]] = {}
    for c in sorted(codes):
        fams.setdefault(_family(c), []).append(c)
    batches: list[list[str]] = []
    current: list[str] = []
    for fam_codes in fams.values():
        for i in range(0, len(fam_codes), size):
            piece = fam_codes[i : i + size]
            if current and len(current) + len(piece) > size:
                batches.append(current)
                current = []
            current.extend(piece)
    if current:
        batches.append(current)
    return batches


def _format_control_group(
    code: str, members: list[tuple[Scenario, ExpectedControl]], known: list[Weakness]
) -> str:
    names = sorted({ec.name for _, ec in members if ec.name})
    descs = sorted({ec.description for _, ec in members if ec.description})
    rats = sorted({ec.rationale for _, ec in members if ec.rationale})
    scen = "; ".join(f"{s.code} — {s.name}" for s, _ in members)
    lines = [f"## control_code: {code}", f"name: {' / '.join(names) or code}"]
    if descs:
        lines.append("description: " + " | ".join(descs))
    if rats:
        lines.append("why it matters here: " + " | ".join(r[:300] for r in rats))
    lines.append(f"protects scenarios: {scen}")
    lines.append("known weaknesses already mapped to this control:")
    lines.append(_format_known_weaknesses(known))
    return "\n".join(lines)


def _format_reported(reported: list[str]) -> str:
    if not reported:
        return ""
    lines = "\n".join(f"- {r}" for r in reported)
    return (
        "# Contradictions already reported under other controls (do NOT report these again; "
        "you may reference them in `rationale`)\n" + lines + "\n\n"
    )


def _build_bundle_messages(
    assessment: Assessment,
    bundle_text: str,
    groups: list[tuple[str, list[tuple[Scenario, ExpectedControl]], list[Weakness]]],
    reported: list[str] | None = None,
) -> list[dict]:
    controls_block = "\n\n".join(_format_control_group(c, m, k) for c, m, k in groups)
    codes = ", ".join(c for c, _, _ in groups)
    user_block = (
        f"{assessment_context_block(assessment)}\n\n"
        f"# Vendor\n{assessment.vendor_name}\n\n"
        f"{bundle_text}\n\n"
        f"{_format_reported(reported or [])}"
        f"# Controls to assess ({len(groups)}): {codes}\n\n{controls_block}\n\n"
        "Assess every control listed above against the whole bundle. Output JSON per schema; "
        "`controls` must contain each control_code exactly once."
    )
    return [
        {"role": "system", "content": BUNDLE_SYSTEM_PROMPT},
        {"role": "user", "content": user_block},
    ]


async def assess_codes_with_bundle(
    db: Session,
    assessment: Assessment,
    codes: list[str],
    *,
    bundle_text: str,
    chunk_index: dict[int, Chunk],
    groups_by_code: dict[str, list[tuple[Scenario, ExpectedControl]]],
    client: LLMClient | None = None,
    reported: list[str] | None = None,
) -> dict[str, ControlAssessmentOut]:
    """One whole-bundle call for a batch of codes (+ one fill call for any code
    the model left out). A call whose output truncates even at the enlarged
    budget is split in half and each half retried (down to single codes).
    Persists each verdict to EVERY scenario expecting the code. Returns the
    verdicts by code; codes still missing after the fill call are absent (the
    caller records them as failed). `reported` (mutable) collects one line per
    contradiction found so later batches can avoid repeating them."""
    model_override = (assessment.model_overrides or {}).get("gap_analysis")
    known_by_code = {c: _known_weaknesses_for_control(db, assessment.id, c) for c in codes}
    if reported is None:
        reported = []

    async def call(batch: list[str], purpose: str) -> dict[str, ControlAssessmentOut]:
        groups = [(c, groups_by_code[c], known_by_code[c]) for c in batch]
        try:
            out: ControlBatchOut = await call_structured(
                db,
                purpose=purpose,
                profile="reasoner",
                messages=_build_bundle_messages(assessment, bundle_text, groups, list(reported)),
                schema=ControlBatchOut,
                assessment_id=assessment.id,
                model_override=model_override,
                max_tokens=settings.llm_budget_large,
                client=client,
            )
        except LLMError as e:
            if not e.truncated or len(batch) < 2:
                raise
            # Output too large for this batch: halve it and assess each part.
            mid = len(batch) // 2
            got = await call(batch[:mid], "gap_analysis_bundle_split")
            got.update(await call(batch[mid:], "gap_analysis_bundle_split"))
            return got
        wanted = set(batch)
        got: dict[str, ControlAssessmentOut] = {}
        for c in out.controls:
            if c.control_code in wanted and c.control_code not in got:
                got[c.control_code] = c
                for con in c.contradictions:
                    reported.append(f"[{c.control_code}] {con.description[:300]}")
        return got

    verdicts = await call(codes, "gap_analysis_bundle")
    missing = [c for c in codes if c not in verdicts]
    if missing:
        verdicts.update(await call(missing, "gap_analysis_bundle_fill"))

    for code, out in verdicts.items():
        for scenario, control in groups_by_code[code]:
            _persist_control_output(
                db, assessment, scenario, control, out, chunk_index, known_by_code[code]
            )
    return verdicts


async def _bundle_batch_worker(
    *,
    assessment_id: int,
    batch: list[str],
    bundle_text: str,
    semaphore: asyncio.Semaphore,
    client: LLMClient | None,
    on_done,
    reported: list[str],
) -> list[str]:
    """Returns the SCENARIO/CONTROL labels that failed in this batch."""
    async with semaphore:
        with SessionLocal() as inner:
            assessment = inner.get(Assessment, assessment_id)
            _, chunks = load_bundle(inner, assessment_id)
            chunk_index = {c.id: c for c in chunks}
            groups_by_code = group_controls_by_code(assessment)
            labels = {
                c: [f"{s.code}/{ec.code}" for s, ec in groups_by_code.get(c, [])] for c in batch
            }
            failed: list[str] = []
            try:
                verdicts = await assess_codes_with_bundle(
                    inner, assessment, batch,
                    bundle_text=bundle_text, chunk_index=chunk_index,
                    groups_by_code=groups_by_code, client=client, reported=reported,
                )
            except Exception as e:
                inner.rollback()
                for c in batch:
                    for _, ec in groups_by_code.get(c, []):
                        record_control_failure(ec.id, e)
                    failed.extend(labels[c])
                    if on_done is not None:
                        on_done(c)
                return failed
            for c in batch:
                if c not in verdicts:
                    err = RuntimeError(f"model returned no verdict for {c} even after a fill call")
                    for _, ec in groups_by_code.get(c, []):
                        record_control_failure(ec.id, err)
                    failed.extend(labels[c])
                if on_done is not None:
                    on_done(c)
    return failed


async def run_full_bundle(
    db: Session,
    assessment: Assessment,
    *,
    client: LLMClient | None = None,
    on_progress=None,
    only_failed: bool = False,
) -> GapAnalysisResult:
    docs, chunks = load_bundle(db, assessment.id)
    bundle_text = format_bundle(docs, chunks)
    groups_by_code = group_controls_by_code(assessment)
    codes: list[str] = []
    for code, members in groups_by_code.items():
        if only_failed:
            pending = [
                ec for _, ec in members
                if ec.assessment is None or ec.assessment.last_error
            ]
            if not pending:
                continue
        codes.append(code)
    total_targets = sum(len(groups_by_code[c]) for c in codes)
    if not codes:
        return GapAnalysisResult(0, [])

    batches = batch_codes(codes)
    semaphore = asyncio.Semaphore(BUNDLE_CONCURRENCY)
    reported: list[str] = []  # shared across batches (see BUNDLE_CONCURRENCY)
    done = 0
    n_codes = len(codes)

    def report_done(code: str) -> None:
        nonlocal done
        done += 1
        if on_progress:
            on_progress(done, n_codes, f"{code} (whole-bundle, {len(groups_by_code[code])} scenario(s))")

    results = await asyncio.gather(
        *[
            _bundle_batch_worker(
                assessment_id=assessment.id, batch=b, bundle_text=bundle_text,
                semaphore=semaphore, client=client, on_done=report_done, reported=reported,
            )
            for b in batches
        ]
    )
    failed_labels = [lbl for batch_failed in results for lbl in batch_failed]
    if failed_labels and len(failed_labels) == total_targets:
        raise RuntimeError(
            f"0/{total_targets} controls assessed; every whole-bundle call failed "
            f"(see ControlAssessment.last_error)."
        )
    return GapAnalysisResult(
        total_targets, [(lbl, RuntimeError("see ControlAssessment.last_error")) for lbl in failed_labels]
    )


async def assess_control_any_mode(
    db: Session,
    assessment: Assessment,
    scenario: Scenario,
    control: ExpectedControl,
    *,
    client: LLMClient | None = None,
) -> None:
    """Per-control (re)assessment used by the API: whole-bundle mode when the
    bundle fits (the verdict is written to every scenario expecting the code,
    keeping verdicts consistent), otherwise the retrieval path."""
    docs, chunks = load_bundle(db, assessment.id)
    if not bundle_fits(chunks):
        await assess_control(db, assessment, scenario, control, client=client)
        return
    groups_by_code = group_controls_by_code(assessment)
    verdicts = await assess_codes_with_bundle(
        db, assessment, [control.code],
        bundle_text=format_bundle(docs, chunks), chunk_index={c.id: c for c in chunks},
        groups_by_code=groups_by_code, client=client,
    )
    if control.code not in verdicts:
        raise RuntimeError(f"model returned no verdict for {control.code}")


async def _assess_control_worker(
    *,
    assessment_id: int,
    scenario_id: int,
    control_id: int,
    semaphore: asyncio.Semaphore,
    client: LLMClient | None,
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
            try:
                await assess_control(inner, assessment, scenario, control, client=client)
            except Exception as e:
                inner.rollback()
                record_control_failure(control_id, e)
                raise
    if on_done is not None:
        on_done(label)


def record_control_failure(control_id: int, err: BaseException) -> None:
    """Persist a failed AI run on the control (R8). The previous verdict, if
    any, is left in place but flagged stale via `last_error`; a control that
    was never assessed gets a row with coverage=none / effectiveness=unknown —
    exactly how the scoring API already treats a missing assessment — so the
    failure changes no score on its own but is visible and resumable."""
    with SessionLocal() as db:
        control = db.get(ExpectedControl, control_id)
        if control is None:
            return
        ca = control.assessment
        if ca is None:
            ca = ControlAssessment(expected_control_id=control.id)
            db.add(ca)
        ca.last_error = f"{type(err).__name__}: {err}"[:2000]
        ca.last_run_at = datetime.utcnow()
        db.commit()


def failed_targets(assessment: Assessment) -> list[str]:
    """SCENARIO/CONTROL labels whose last AI run failed."""
    out: list[str] = []
    for s in assessment.scenarios:
        for ctrl in s.expected_controls:
            ca = ctrl.assessment
            if ca is not None and ca.last_error:
                out.append(f"{s.code}/{ctrl.code}")
    return out


class GapAnalysisResult:
    """Outcome of run_full: which targets failed (persisted per control) and
    a human summary. A partial failure is NOT an exception any more — the
    phase completes, the failed controls are visible on the UI and can be
    re-run individually or all at once with only_failed=True."""

    def __init__(self, total: int, failed: list[tuple[str, BaseException]]):
        self.total = total
        self.failed = failed

    @property
    def failed_labels(self) -> list[str]:
        return [lbl for lbl, _ in self.failed]

    @property
    def warning(self) -> str | None:
        if not self.failed:
            return None
        first_label, first_err = self.failed[0]
        return (
            f"{self.total - len(self.failed)}/{self.total} controls assessed; "
            f"{len(self.failed)} failed and can be re-run "
            f"({', '.join(self.failed_labels[:6])}{'…' if len(self.failed) > 6 else ''}). "
            f"First failure on '{first_label}': {str(first_err)[:200]}"
        )


async def run_full(
    db: Session,
    assessment: Assessment,
    *,
    client: LLMClient | None = None,
    on_progress=None,
    only_failed: bool = False,
) -> GapAnalysisResult:
    """Iterate every scenario × control and run gap analysis (parallel, bounded).

    only_failed=True resumes a previous run: only controls whose last AI run
    failed (ControlAssessment.last_error set) or that have never been
    assessed are (re)assessed; everything else is left untouched.
    """
    _, all_chunks = load_bundle(db, assessment.id)
    if bundle_fits(all_chunks):
        return await run_full_bundle(
            db, assessment, client=client, on_progress=on_progress, only_failed=only_failed
        )

    # ---- fallback: per-control retrieval mode (oversized bundle) ----
    # Snapshot ids from the parent session before fanning out — workers will
    # re-load the rows in their own sessions.
    targets: list[tuple[int, int, str]] = []
    for s in assessment.scenarios:
        for ctrl in s.expected_controls:
            if only_failed:
                ca = ctrl.assessment
                if ca is not None and not ca.last_error:
                    continue
            targets.append((s.id, ctrl.id, f"{s.code}/{ctrl.code}"))
    total = len(targets)
    if total == 0:
        return GapAnalysisResult(0, [])

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
    if failed and len(failed) == total:
        # Nothing succeeded: that is a run-level failure (bad key, model
        # down…), not a per-control one — fail loudly as before.
        first_label, first_err = failed[0]
        msg = (
            f"0/{total} controls assessed; every call failed. "
            f"First failure on '{first_label}': {first_err}"
        )
        if isinstance(first_err, LLMError):
            raise LLMError(
                msg, transient=first_err.transient, upstream_code=first_err.upstream_code
            )
        raise RuntimeError(msg)
    return GapAnalysisResult(total, failed)
