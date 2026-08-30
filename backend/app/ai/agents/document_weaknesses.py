"""Per-document weakness extraction.

Strategy:
    Default — ONE reasoner call with the full document text. Fast, cheap, and
    most accurate when the doc fits because the model sees the whole picture.

    Adaptive fallback — only when the input would exceed `SINGLE_CALL_MAX_TOKENS`
    OR the default call returned `finish_reason=length`.

    Questionnaires (R8) fall back to *windowed direct extraction*: the sheets /
    domains (section_paths) are packed into windows under
    `QUESTIONNAIRE_WINDOW_MAX_TOKENS`, each window is extracted with the
    ordinary questionnaire prompt, and rows are dedup-guarded at the DB. Every
    row is seen exactly once, so nothing is ever dropped by a finding cap.

    Every other kind falls back to a two-phase, structure-aware extraction:
        Phase 1 — kind-aware enumeration call(s) returning weakness skeletons
                  (heading + severity + section_path + kind_signal). Output is
                  small so truncation risk is low. If even the enumeration
                  exceeds the input ceiling, sections are packed into windows
                  that NEVER split a section across a boundary (preserving each
                  finding's heading + body together) and each window prepends
                  the doc's preamble (exec summary / TOC / severity legend) so
                  cross-references and severity calibration stay consistent.
                  Skeletons are deduped by normalised heading.
        Phase 2 — one detail call per skeleton, scoped to that section + the
                  preamble. Bounded input, small output. Calls run in parallel
                  under `asyncio.Semaphore(PHASE2_CONCURRENCY)` with each
                  worker using its own `SessionLocal()` (mirrors scenarios
                  phase-2 pattern) so concurrent commits don't race.

Persistence is dedup-guarded at the DB level (uq_weakness_dedupe), so a
document re-extracted, retried after a partial failure, or whose preamble
overlaps another window cannot insert the same finding twice.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.context import analysis_datetime, standards_block
from app.ai.prompts import load as load_prompt
from app.ai.router import OpenRouterClient, OpenRouterError, call_structured
from app.db import SessionLocal
from app.models import Chunk, Document, Weakness
from app.schemas.ai import (
    DocumentWeaknessListOut,
    DocumentWeaknessOut,
    WeaknessSkeletonListOut,
    WeaknessSkeletonOut,
)

ProgressCb = Callable[[float, str], Awaitable[None]]

# Approx-token budgets. We use len(text)//4 as a cheap estimator (no tiktoken
# dependency). Modern reasoners handle far more, but the goal here is to fall
# back to two-phase well before the model starts dropping recall in the long
# tail of context — not to push the absolute ceiling.
SINGLE_CALL_MAX_TOKENS = 80_000
ENUMERATE_INPUT_MAX = 150_000

PHASE2_CONCURRENCY = 4
# Questionnaire windows: small enough that the per-window output (one row per
# negative answer) stays far below the output budget even on dense sheets.
QUESTIONNAIRE_WINDOW_MAX_TOKENS = 12_000

_PROMPT_BY_KIND: dict[str, str] = {
    "pentest": "document_weaknesses_pentest",
    "soc": "document_weaknesses_soc",
    "iso": "document_weaknesses_iso",
    "questionnaire": "document_weaknesses_questionnaire",
    "policy": "document_weaknesses_policy",
    "other": "document_weaknesses_other",
}

_ENUMERATE_KIND_CONTEXT: dict[str, str] = {
    "pentest": (
        "This is a penetration test report. A finding is anything labelled "
        "'Finding F-N', 'Issue', 'Observation', 'Risk', or similar. Use the "
        "report's own severity verbatim where possible. Use kind_signal "
        "\"pentest_finding\"."
    ),
    "soc": (
        "This is a SOC 2 report. A finding is a Section IV testing exception, "
        "a qualified opinion, a scope carve-out, or a CUEC. Use kind_signal "
        "\"soc_exception\"."
    ),
    "iso": (
        "This is an ISO/IEC 27001 audit / SoA. A finding is a major or minor "
        "nonconformity, an observation, a scope exclusion, or an SoA gap. "
        "Use kind_signal \"iso_nonconformity\"."
    ),
    "questionnaire": (
        "This is a vendor security questionnaire. A finding is an explicit "
        "\"no\", a hedged answer, a missing answer, or a yes-with-qualifier "
        "on a control question. Use kind_signal \"questionnaire_negative\"."
    ),
    "policy": (
        "This is a vendor policy document. A finding is a baseline element "
        "absent or vague (no SLA, no cadence, no owner) for the policy's "
        "topic. Use kind_signal \"policy_gap\"."
    ),
    "other": (
        "This is a vendor document of unspecified type (often DPA / MSA / "
        "whitepaper / attestation). A finding is a missing expected clause, "
        "a vague obligation, or an explicit limitation. Use kind_signal "
        "\"dpa_clause_missing\" for DPA-style omissions and \"other\" otherwise."
    ),
}


def _approx_tokens(text: str) -> int:
    return len(text) // 4


def _temporal_header(
    analysis_dt: datetime, uploaded_dt: datetime | None, standards: str = ""
) -> str:
    """Analysis date (= assessment.as_of_date, R7), upload date relative to
    it, and the assessor standards block."""
    analysis_line = f"# Analysis date: {analysis_dt.strftime('%Y-%m-%d')}"
    parts = [analysis_line]
    if uploaded_dt is None:
        parts.append("# Document uploaded: unknown")
    else:
        delta = (analysis_dt.date() - uploaded_dt.date()).days
        if delta >= 0:
            parts.append(
                f"# Document uploaded: {uploaded_dt.strftime('%Y-%m-%d')} "
                f"({delta} days before the analysis date)"
            )
        # An upload that post-dates a pinned analysis date is a run artefact,
        # not evidence: it is withheld entirely so no freshness judgement can
        # be anchored on it (the canary showed the model still using it when
        # it was merely labelled). Freshness then rests on the document's own
        # dates versus the analysis date.
    parts.append(
        "# Freshness rule: judge staleness ONLY against the analysis date above, "
        "using the document's own dates (version, period end, test date)."
    )
    if standards:
        parts.append(standards)
    return "\n".join(parts)


def _normalise(text: str) -> str:
    """Lower-case, punctuation-free, single-spaced (see gap_analysis._norm):
    markdown markers and typographic dashes in chunk text must not stop a
    verbatim quote from binding to its chunk."""
    return re.sub(r"[^0-9a-z]+", " ", text.lower()).strip()


def _dedupe_key(assessment_id: int, quote: str, chunk_id: int | None) -> str:
    payload = f"{assessment_id}|{_normalise(quote)[:200]}|{chunk_id or 0}"
    return hashlib.sha256(payload.encode()).hexdigest()[:120]


def _render_chunks(chunks: list[Chunk]) -> str:
    """Render chunks with markers the model uses to cite sections + pages."""
    parts: list[str] = []
    for c in chunks:
        loc_bits = []
        if c.page is not None:
            loc_bits.append(f"page {c.page}")
        if c.section_path:
            loc_bits.append(c.section_path)
        loc = " — ".join(loc_bits) if loc_bits else "section unknown"
        parts.append(
            f"[chunk_id={c.id} section_path=\"{c.section_path}\" {loc}]\n{c.text}"
        )
    return "\n\n---\n\n".join(parts)


def _build_preamble(chunks: list[Chunk], target_tokens: int = 1500) -> str:
    """Take the first chunks up to ~target_tokens. Captures TOC, exec summary,
    and severity legend in most TPRM-relevant document layouts."""
    if not chunks:
        return ""
    parts: list[str] = []
    used = 0
    for c in chunks:
        seg = f"[chunk_id={c.id} section_path=\"{c.section_path}\"]\n{c.text}"
        seg_tokens = _approx_tokens(seg)
        if parts and used + seg_tokens > target_tokens:
            break
        parts.append(seg)
        used += seg_tokens
    return "\n\n---\n\n".join(parts)


def _group_by_section(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    """Group chunks by section_path while preserving chunk.ord order."""
    groups: dict[str, list[Chunk]] = defaultdict(list)
    for c in sorted(chunks, key=lambda x: x.ord):
        groups[c.section_path or "(root)"].append(c)
    return groups


def _render_window(preamble: str, chunks: list[Chunk]) -> str:
    body = _render_chunks(chunks)
    if preamble:
        return (
            "# Document preamble (exec summary / TOC / severity legend)\n"
            f"{preamble}\n\n"
            "# Document sections in this window\n"
            f"{body}"
        )
    return f"# Document sections in this window\n{body}"


def _pack_sections_into_windows(
    sections: dict[str, list[Chunk]],
    preamble: str,
    cap_tokens: int,
) -> list[str]:
    """Pack contiguous sections into windows whose total tokens stay under
    `cap_tokens`. Sections are NEVER split — they're the atomic unit. Each
    window includes the preamble so cross-references / severity legend stay
    available."""
    preamble_tokens = _approx_tokens(preamble) if preamble else 0
    windows: list[str] = []
    current: list[Chunk] = []
    current_tokens = preamble_tokens
    for sec_chunks in sections.values():
        sec_text = _render_chunks(sec_chunks)
        sec_tokens = _approx_tokens(sec_text)
        if current and current_tokens + sec_tokens > cap_tokens:
            windows.append(_render_window(preamble, current))
            current = []
            current_tokens = preamble_tokens
        current.extend(sec_chunks)
        current_tokens += sec_tokens
    if current:
        windows.append(_render_window(preamble, current))
    return windows or [_render_window(preamble, [])]


def _resolve_chunk_id(
    chunks: list[Chunk], section_path: str, page: int | None, quote: str
) -> int | None:
    """Locate the most likely source chunk for a weakness output."""
    quote_norm = _normalise(quote)[:60] if quote else ""
    candidates: list[Chunk] = chunks
    if section_path:
        sec_match = [c for c in chunks if c.section_path == section_path]
        if sec_match:
            candidates = sec_match
    if quote_norm:
        for c in candidates:
            if quote_norm in _normalise(c.text):
                return c.id
    if page is not None:
        for c in candidates:
            if c.page == page:
                return c.id
    if candidates is not chunks and candidates:
        return candidates[0].id
    return None


def _build_weakness_row(
    *,
    w: DocumentWeaknessOut,
    assessment_id: int,
    document_id: int,
    chunks: list[Chunk],
) -> Weakness:
    chunk_id = _resolve_chunk_id(chunks, w.section_path, w.page, w.quote)
    return Weakness(
        assessment_id=assessment_id,
        source_chunk_id=chunk_id,
        source_document_id=document_id,
        severity=w.severity,
        description=w.description,
        mapped_control_codes=list(w.suggested_control_codes),
        quote=w.quote or "",
        unmatched=True,
        kind_signal=w.kind_signal,
        dedupe_key=_dedupe_key(assessment_id, w.quote, chunk_id),
    )


def _persist_rows(rows: list[Weakness]) -> int:
    """Insert rows one-by-one in a fresh session; uniqueness clashes are
    swallowed (the same finding extracted twice across overlapping windows
    is the expected case)."""
    if not rows:
        return 0
    inserted = 0
    with SessionLocal() as inner:
        for r in rows:
            try:
                inner.add(r)
                inner.commit()
                inserted += 1
            except IntegrityError:
                inner.rollback()
    return inserted


def _enumerate_prompt(kind: str) -> str:
    template = load_prompt("document_weaknesses_enumerate")
    ctx = _ENUMERATE_KIND_CONTEXT.get(kind, _ENUMERATE_KIND_CONTEXT["other"])
    return template.replace("{{KIND_CONTEXT}}", ctx)


def _kind_prompt(kind: str) -> str:
    name = _PROMPT_BY_KIND.get(kind, "document_weaknesses_other")
    return load_prompt(name)


def _pack_section_windows(
    sections: dict[str, list[Chunk]], cap_tokens: int
) -> list[list[Chunk]]:
    """Contiguous sections packed into windows of ≤ cap_tokens (sections are
    never split; an oversized single section becomes its own window)."""
    windows: list[list[Chunk]] = []
    current: list[Chunk] = []
    current_tokens = 0
    for sec_chunks in sections.values():
        sec_tokens = _approx_tokens(_render_chunks(sec_chunks))
        if current and current_tokens + sec_tokens > cap_tokens:
            windows.append(current)
            current, current_tokens = [], 0
        current.extend(sec_chunks)
        current_tokens += sec_tokens
    if current:
        windows.append(current)
    return windows


async def _extract_questionnaire_windowed(
    db: Session,
    *,
    doc: Document,
    chunks: list[Chunk],
    header: str,
    vendor_name: str,
    prompt_kind: str,
    assessment_id: int,
    model_override: str | None,
    client: OpenRouterClient | None,
    on_progress: ProgressCb | None,
) -> int:
    """Windowed direct extraction for long questionnaires (R8): one ordinary
    extraction call per window of sheets/domains; no enumeration, no cap."""
    windows = _pack_section_windows(_group_by_section(chunks), QUESTIONNAIRE_WINDOW_MAX_TOKENS)
    n = len(windows)
    inserted = 0
    failures: list[BaseException] = []
    for i, win in enumerate(windows):
        first_sec = win[0].section_path or "(root)"
        last_sec = win[-1].section_path or "(root)"
        user_block = (
            f"{header}\n"
            f"# Vendor service: {vendor_name}\n"
            f"# Document: {doc.filename} (kind: {doc.kind})\n"
            f"# Window {i + 1} of {n}: sections \"{first_sec}\" … \"{last_sec}\" "
            "(other windows are extracted separately — report only what is in this window)\n\n"
            f"{_render_chunks(win)}"
        )
        try:
            out: DocumentWeaknessListOut = await call_structured(
                db,
                purpose="document_weakness_extract_window",
                profile="reasoner",
                messages=[
                    {"role": "system", "content": prompt_kind},
                    {"role": "user", "content": user_block},
                ],
                schema=DocumentWeaknessListOut,
                assessment_id=assessment_id,
                model_override=model_override,
                max_tokens=8192,
                client=client,
            )
        except OpenRouterError as e:
            failures.append(e)
            continue
        rows = [
            _build_weakness_row(
                w=w, assessment_id=assessment_id, document_id=doc.id, chunks=chunks
            )
            for w in out.weaknesses
        ]
        inserted += _persist_rows(rows)
        if on_progress:
            await on_progress(
                0.15 + 0.85 * (i + 1) / n,
                f"Extracted {inserted} weaknesses ({i + 1}/{n} windows)",
            )
    if failures:
        first = failures[0]
        raise OpenRouterError(
            f"{n - len(failures)}/{n} questionnaire windows extracted; "
            f"{len(failures)} failed. First failure: {first}",
            transient=getattr(first, "transient", False),
            upstream_code=getattr(first, "upstream_code", None),
        )
    return inserted


async def extract(
    db: Session,
    document_id: int,
    *,
    on_progress: ProgressCb | None = None,
    client: OpenRouterClient | None = None,
) -> int:
    """Extract weaknesses for a single document.

    Returns the number of distinct weaknesses inserted. Stamps
    `Document.weakness_extracted_at` on completion regardless of the path
    taken.
    """
    doc = db.get(Document, document_id)
    if doc is None:
        raise ValueError(f"Document {document_id} not found")
    chunks = sorted(doc.chunks, key=lambda c: c.ord)
    if not chunks:
        doc.weakness_extracted_at = datetime.utcnow()
        db.commit()
        return 0

    # Capture all scalars we need so phase-2 workers (different sessions)
    # don't lazy-load on a session that's already in their parent task.
    assessment_id = doc.assessment_id
    vendor_name = doc.assessment.vendor_name if doc.assessment else ""
    model_overrides = doc.assessment.model_overrides if doc.assessment else None
    model_override = (model_overrides or {}).get("weaknesses")

    kind = doc.kind if doc.kind in _PROMPT_BY_KIND else "other"
    prompt_kind = _kind_prompt(kind)

    # Analysis date = assessment.as_of_date (R7), never the wall clock.
    analysis_dt = analysis_datetime(doc.assessment)
    temporal_header = _temporal_header(
        analysis_dt, doc.created_at, standards_block(doc.assessment)
    )

    full_text = _render_chunks(chunks)
    full_input = (
        f"{temporal_header}\n"
        f"# Vendor service: {vendor_name}\n"
        f"# Document: {doc.filename} (kind: {doc.kind})\n\n"
        f"{full_text}"
    )
    approx = _approx_tokens(full_input)

    # ---- Default path: single call ----
    if approx <= SINGLE_CALL_MAX_TOKENS:
        if on_progress:
            await on_progress(0.1, f"Extracting from {doc.filename} (single call)...")
        try:
            out: DocumentWeaknessListOut = await call_structured(
                db,
                purpose="document_weakness_extract",
                profile="reasoner",
                messages=[
                    {"role": "system", "content": prompt_kind},
                    {"role": "user", "content": full_input},
                ],
                schema=DocumentWeaknessListOut,
                assessment_id=assessment_id,
                model_override=model_override,
                max_tokens=8192,
                client=client,
            )
            rows = [
                _build_weakness_row(
                    w=w,
                    assessment_id=assessment_id,
                    document_id=document_id,
                    chunks=chunks,
                )
                for w in out.weaknesses
            ]
            inserted = _persist_rows(rows)
            doc.weakness_extracted_at = datetime.utcnow()
            db.commit()
            if on_progress:
                await on_progress(1.0, f"Extracted {inserted} weaknesses (single call)")
            return inserted
        except OpenRouterError as e:
            if not e.truncated:
                raise
            # Fall through to two-phase fallback.
            if on_progress:
                await on_progress(
                    0.15,
                    f"Output truncated; falling back to two-phase extraction...",
                )

    # ---- Questionnaire fallback: windowed direct extraction, no cap ----
    if kind == "questionnaire":
        inserted = await _extract_questionnaire_windowed(
            db,
            doc=doc,
            chunks=chunks,
            header=temporal_header,
            vendor_name=vendor_name,
            prompt_kind=prompt_kind,
            assessment_id=assessment_id,
            model_override=model_override,
            client=client,
            on_progress=on_progress,
        )
        doc.weakness_extracted_at = datetime.utcnow()
        db.commit()
        if on_progress:
            await on_progress(1.0, f"Extracted {inserted} weaknesses (windowed)")
        return inserted

    # ---- Two-phase fallback (all other kinds) ----
    sections = _group_by_section(chunks)
    preamble = _build_preamble(chunks)
    enum_prompt = _enumerate_prompt(kind)

    if approx <= ENUMERATE_INPUT_MAX:
        windows = [_render_window(preamble, chunks)]
    else:
        windows = _pack_sections_into_windows(sections, preamble, ENUMERATE_INPUT_MAX)

    skeletons: list[WeaknessSkeletonOut] = []
    for i, window_text in enumerate(windows):
        out: WeaknessSkeletonListOut = await call_structured(
            db,
            purpose="document_weakness_enumerate",
            profile="reasoner",
            messages=[
                {"role": "system", "content": enum_prompt},
                {"role": "user", "content": window_text},
            ],
            schema=WeaknessSkeletonListOut,
            assessment_id=assessment_id,
            model_override=model_override,
            max_tokens=4096,
            client=client,
        )
        skeletons.extend(out.skeletons)
        if on_progress:
            await on_progress(
                0.15 + 0.25 * (i + 1) / len(windows),
                f"Enumerated {len(skeletons)} findings ({i + 1}/{len(windows)} windows)",
            )

    # Dedupe by normalised heading — protects against overlap between adjacent
    # windows when the same finding's heading appears in both windows' shared
    # preamble.
    deduped: dict[str, WeaknessSkeletonOut] = {}
    for sk in skeletons:
        key = _normalise(sk.heading)
        if key not in deduped:
            deduped[key] = sk
    skeletons = list(deduped.values())
    # No finding cap (R8): every skeleton is detailed. Concurrency bounds the
    # load; nothing is silently dropped.

    if not skeletons:
        doc.weakness_extracted_at = datetime.utcnow()
        db.commit()
        if on_progress:
            await on_progress(1.0, "No findings.")
        return 0

    # Phase 2: parallel detail calls. Each worker uses its own SessionLocal
    # so call_structured's ModelCall logging and our weakness inserts don't
    # race on a shared session.
    section_text_by_path = {
        sec_path: _render_chunks(secs) for sec_path, secs in sections.items()
    }
    semaphore = asyncio.Semaphore(PHASE2_CONCURRENCY)
    n = len(skeletons)
    done = 0

    async def detail_worker(sk: WeaknessSkeletonOut) -> int:
        nonlocal done
        async with semaphore:
            sec_text = (
                section_text_by_path.get(sk.section_path)
                or section_text_by_path.get(sk.section_path or "(root)")
                or ""
            )
            user_block = (
                f"{temporal_header}\n"
                f"# Vendor service: {vendor_name}\n"
                f"# Document: {doc.filename} (kind: {doc.kind})\n\n"
                f"# Document preamble\n{preamble}\n\n"
                f"# Skeleton heading\n{sk.heading} (severity={sk.severity})\n\n"
                f"# Section content\n{sec_text}\n\n"
                "Return the weakness for this finding only — do not include other findings."
            )
            with SessionLocal() as inner:
                out: DocumentWeaknessListOut = await call_structured(
                    inner,
                    purpose="document_weakness_detail",
                    profile="reasoner",
                    messages=[
                        {"role": "system", "content": prompt_kind},
                        {"role": "user", "content": user_block},
                    ],
                    schema=DocumentWeaknessListOut,
                    assessment_id=assessment_id,
                    model_override=model_override,
                    max_tokens=2048,
                    client=client,
                )
                rows = [
                    _build_weakness_row(
                        w=w,
                        assessment_id=assessment_id,
                        document_id=document_id,
                        chunks=chunks,
                    )
                    for w in out.weaknesses
                ]
                count = 0
                for r in rows:
                    try:
                        inner.add(r)
                        inner.commit()
                        count += 1
                    except IntegrityError:
                        inner.rollback()
        done += 1
        if on_progress:
            await on_progress(
                0.4 + 0.6 * done / n,
                f"Detailed {done}/{n} findings",
            )
        return count

    results = await asyncio.gather(
        *[detail_worker(sk) for sk in skeletons], return_exceptions=True
    )
    inserted = sum(r for r in results if isinstance(r, int))
    failures = [r for r in results if isinstance(r, BaseException)]

    doc.weakness_extracted_at = datetime.utcnow()
    db.commit()

    if failures:
        first_err = failures[0]
        codes = [sk.heading for sk, r in zip(skeletons, results, strict=True) if isinstance(r, BaseException)]
        msg = (
            f"{inserted}/{n} findings detailed; {len(failures)} failed "
            f"({', '.join(codes[:3])}{'...' if len(codes) > 3 else ''}). "
            f"First failure: {first_err}"
        )
        raise OpenRouterError(
            msg,
            transient=getattr(first_err, "transient", False),
            upstream_code=getattr(first_err, "upstream_code", None),
        )

    if on_progress:
        await on_progress(1.0, f"Extracted {inserted} weaknesses (two-phase)")
    return inserted
