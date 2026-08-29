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
import hashlib

from sqlalchemy.exc import IntegrityError
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
    Weakness,
)
from app.schemas.ai import CitationOut, ControlAssessmentOut
from app.scoring.engine import _META_WEIGHTS

CONTRADICTION_KIND = "cross_doc_conflict"
GAP_ANALYSIS_ORIGIN = "gap_analysis"
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}

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


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def _quote_in_chunk(quote: str, chunk_text: str) -> bool:
    """True when the (normalized) quote genuinely appears in the chunk.

    Falls back to a long prefix for quotes that straddle a chunk boundary —
    but never to anything shorter, so a match is always a real occurrence.
    """
    q = _norm(quote)
    if len(q) < 10:
        return False  # too short to be a trustworthy anchor
    t = _norm(chunk_text)
    return q in t or (len(q) > 60 and q[:60] in t)


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
    client: OpenRouterClient | None = None,
) -> ControlAssessmentOut:
    chunks = _retrieve_for_control(db, assessment.id, control, scenario)
    known = _known_weaknesses_for_control(db, assessment.id, control.code)
    model_override = (assessment.model_overrides or {}).get("gap_analysis")

    out: ControlAssessmentOut = await call_structured(
        db,
        purpose="gap_analysis_control",
        profile="reasoner",
        messages=_build_messages(scenario, control, chunks, known_weaknesses=known),
        schema=ControlAssessmentOut,
        assessment_id=assessment.id,
        model_override=model_override,
        max_tokens=4096,
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
                    scenario, control, chunks, known_weaknesses=known, second_pass=True
                ),
                schema=ControlAssessmentOut,
                assessment_id=assessment.id,
                model_override=model_override,
                max_tokens=4096,
                client=client,
            )

    chunk_index = {c.id: c for c in chunks}

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
