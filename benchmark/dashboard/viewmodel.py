"""View-model assembly for the dashboard: pure functions over the results DB
and the golden keys. No HTTP, no templates — everything here is unit-testable.

Nothing in this module changes a score. It only *reads* what the judge and the
metrics layer persisted and derives display statuses from it; where a display
status disagrees with the scored outcome (e.g. a JUDGE_FN is a real hit the
matcher missed, but is *scored* as a miss) the status text says so.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from bench.cases import GoldenExpectations
from bench.config import settings
from bench.costing import (
    STAGE_ORDER,
    AssessmentLedger,
    Money,
    case_cost,
    case_time_s,
    judge_cost,
    judge_time_s,
)
from bench.metrics import BAND_RANK, SEVERITY_RANK
from bench.models import CaseResult, FindingMatch, JudgeCall, Run

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Golden-finding display statuses. Precedence is implemented in finding_status.
# (glyph, short label, legend sentence). Never colour alone: the glyph + label
# carry the meaning.
STATUS_LABELS: dict[str, tuple[str, str, str]] = {
    "identified": ("✓", "Identified",
                   "Reported by the app and confirmed by the matcher — counted as a hit."),
    "identified_optional": ("✓", "Identified (optional)",
                            "Optional golden reported and matched — not scored either way."),
    "identified_by_judge": ("J", "Identified · matcher missed",
                            "The classifier found a reported weakness that covers this golden, "
                            "but the matcher did not pair them (JUDGE_FN). Scored as a miss."),
    "low_confidence": ("~", "Low-confidence match",
                       "The matcher paired it below the confidence threshold — "
                       "scored as a miss plus a false positive."),
    "false_match": ("✗", "False match",
                    "The matcher paired it but the classifier rejects the pairing "
                    "(JUDGE_FP_MATCH). Still scored as a hit."),
    "missed_reasoning": ("●", "Missed · fact in evidence",
                         "Not reported although the fact is present in the ingested "
                         "chunks — an extraction / reasoning miss."),
    "missed_ingestion": ("○", "Missed · fact not in evidence",
                         "Not reported and the fact was not in the ingested chunks — "
                         "an ingestion gap, not a reasoning one."),
    "missed": ("–", "Missed", "Not reported by the app — counted as a miss."),
    "optional_missed": ("·", "Optional · not found",
                        "Optional golden not reported — not scored."),
    "not_in_key": ("?", "Not in key at grading",
                   "This golden id was not part of the answer key when the run was graded."),
    "not_graded": ("∅", "Not graded", "The case ran without the matching judge."),
    "error": ("!", "Error", "The case failed (pipeline or judge error)."),
    "not_assessed": ("", "Not assessed", "The vendor was not part of this run."),
}

HIT_STATUSES = frozenset({"identified", "identified_optional", "identified_by_judge",
                          "false_match", "low_confidence"})
MISS_STATUSES = frozenset({"missed", "missed_reasoning", "missed_ingestion", "optional_missed"})
# Statuses that say nothing about the finding itself; excluded from "differs" checks.
NEUTRAL_STATUSES = frozenset({"not_assessed"})

# Legend order (matches the matrix legend partial).
STATUS_ORDER = (
    "identified", "identified_optional", "identified_by_judge", "false_match",
    "low_confidence", "missed_reasoning", "missed_ingestion", "missed", "optional_missed",
    "not_in_key", "not_graded", "error", "not_assessed",
)

# Classifier categories for reported weaknesses that are *not* a golden hit
# (see bench/prompts.py). Order = stacked-bar order.
EXTRA_CATEGORIES = ("LEGIT_UNKEYED", "DUP_OF_TP", "BOILERPLATE", "MISREAD", "JUDGE_FP_MATCH")
EXTRA_LABELS = {
    "LEGIT_UNKEYED": "legit, not in key",
    "DUP_OF_TP": "duplicate of a hit",
    "BOILERPLATE": "boilerplate",
    "MISREAD": "misread evidence",
    "JUDGE_FP_MATCH": "false match",
}

CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0, None: 0, "": 0}
MAX_COMPARE_RUNS = 8


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def parse_json(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _model_label(run: Run) -> str:
    """Short human label for the model config: overrides if set, else defaults."""
    try:
        models = json.loads(run.models_json or "{}")
    except json.JSONDecodeError:
        return "?"
    overrides = models.get("overrides") or {}
    if overrides:
        uniq = sorted(set(overrides.values()))
        label = ", ".join(m.split("/")[-1] for m in uniq)
        return f"pinned: {label}"
    profiles = models.get("profiles") or []
    parts = [f"{p['name']}={p['default_model'].split('/')[-1]}" for p in profiles]
    return ", ".join(parts) or "defaults"


def _sum_or_none(values: list) -> float | None:
    """Sum of the priced entries; None when nothing was priced at all."""
    priced = [v for v in values if v is not None]
    return sum(priced) if priced else None


def _run_summary(run: Run, results: list[CaseResult]) -> dict:
    """Legacy JSON shape of /api/runs entries (kept verbatim for compatibility)."""
    ok = [r for r in results if r.status == "ok"]
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "status": run.status,
        "app_git_sha": (run.app_git_sha or "")[:7],
        "app_git_dirty": run.app_git_dirty,
        "app_version": run.app_version,
        "model_label": _model_label(run),
        "judge_model": run.judge_model,
        "notes": run.notes,
        "n_cases": len(results),
        "n_ok": len(ok),
        "mean_f1": _mean([r.f1 for r in ok]),
        "mean_exec": _mean([r.exec_overall for r in ok]),
    }


def model_profiles(run: Run) -> list[tuple[str, str]]:
    """[(profile, model)] as configured for the run, overrides applied."""
    models = parse_json(run.models_json, {}) or {}
    overrides = models.get("overrides") or {}
    out = []
    for p in models.get("profiles") or []:
        name = p.get("name", "?")
        model = overrides.get(name) or p.get("default_model", "?")
        out.append((name, model))
    for name, model in overrides.items():
        if name not in {n for n, _ in out}:
            out.append((name, model))
    return out


def run_mode(run: Run) -> str:
    """`run` (fresh assessments) or `grade` (re-grades stored assessments).
    Early runs predate the field."""
    cfg = parse_json(run.config_json, {}) or {}
    return cfg.get("mode") or "run"


def judge_mode_of(run: Run, results: Iterable[CaseResult]) -> str:
    """Config value, else inferred from what was persisted."""
    cfg = parse_json(run.config_json, {}) or {}
    if cfg.get("judge_mode"):
        return cfg["judge_mode"]
    results = list(results)
    if any(cr.classification_json for cr in results):
        return "full"
    if any(cr.tp is not None for cr in results):
        return "match"
    return "none"


def wallclock_s(run: Run) -> float | None:
    if run.started_at and run.finished_at:
        return (run.finished_at - run.started_at).total_seconds()
    return None


def severity_mismatch(expected: str | None, actual: str | None) -> str | None:
    """'up' when the app rated it more severe than the key, 'down' when less."""
    if not expected or not actual or expected == actual:
        return None
    e, a = SEVERITY_RANK.get(expected), SEVERITY_RANK.get(actual)
    if e is None or a is None:
        return "diff"
    return "up" if a > e else "down"


def band_error_of(actual: str | None, expected: str | None) -> int | None:
    if actual in BAND_RANK and expected in BAND_RANK:
        return BAND_RANK[actual] - BAND_RANK[expected]
    return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Golden keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Golden:
    id: str
    description: str
    severity: str | None
    optional: bool = False
    retired: bool = False  # seen in finding_match but no longer in case.yaml


@dataclass
class CaseInfo:
    case_id: str
    vendor_name: str
    expected_band: str | None
    goldens: list[Golden] = field(default_factory=list)
    load_error: str | None = None

    @property
    def required(self) -> int:
        return sum(1 for g in self.goldens if not g.optional and not g.retired)


def load_golden_index(cases_dir: str | Path | None = None) -> dict[str, CaseInfo]:
    """Lenient, yaml-only key loader. Unlike `bench.cases.load_case` it does
    not require the document files to exist and never raises: a case whose
    yaml cannot be read/validated is returned with `load_error` set."""
    root = Path(cases_dir or settings.CASES_DIR)
    index: dict[str, CaseInfo] = {}
    if not root.exists():
        return index
    for case_dir in sorted(p for p in root.iterdir() if (p / "case.yaml").exists()):
        case_id = case_dir.name
        try:
            raw = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8")) or {}
            golden = GoldenExpectations.model_validate(raw.get("golden") or {})
            index[raw.get("id") or case_id] = CaseInfo(
                case_id=raw.get("id") or case_id,
                vendor_name=raw.get("vendor_name") or case_id,
                expected_band=golden.expected_band,
                goldens=[
                    Golden(w.id, w.description, w.severity, w.optional)
                    for w in golden.expected_weaknesses
                ],
            )
        except Exception as e:  # yaml / pydantic — surfaced on the page, never fatal
            index[case_id] = CaseInfo(case_id, case_id, None, [], load_error=str(e))
    return index


def with_legacy_goldens(
    index: dict[str, CaseInfo], matches_by_case: dict[str, Iterable[FindingMatch]]
) -> dict[str, CaseInfo]:
    """Append golden ids that appear in finding_match for a case but not in
    its current yaml key (retired / renamed ids), so history stays visible."""
    out: dict[str, CaseInfo] = {}
    for case_id, fms in matches_by_case.items():
        info = index.get(case_id) or CaseInfo(case_id, case_id, None, [])
        known = {g.id for g in info.goldens}
        extra: dict[str, Golden] = {}
        for fm in fms:
            if fm.match_type == "extra" or not fm.expected_id or fm.expected_id in known:
                continue
            if fm.expected_id not in extra:
                extra[fm.expected_id] = Golden(
                    fm.expected_id, fm.expected_description, fm.expected_severity,
                    bool(fm.expected_optional), retired=True,
                )
        out[case_id] = CaseInfo(
            info.case_id, info.vendor_name, info.expected_band,
            list(info.goldens) + sorted(extra.values(), key=lambda g: g.id),
            info.load_error,
        )
    for case_id, info in index.items():
        out.setdefault(case_id, info)
    return out


# ---------------------------------------------------------------------------
# Finding status (one golden × one case result)
# ---------------------------------------------------------------------------


@dataclass
class CellDetail:
    status: str
    golden_id: str
    label: str = ""
    glyph: str = ""
    optional: bool = False
    retired: bool = False
    case_result_id: int | None = None
    run_id: int | None = None
    case_id: str | None = None
    repetition: int | None = None
    expected_description: str = ""
    expected_severity: str | None = None
    actual_weakness_id: int | None = None
    actual_description: str = ""
    actual_severity: str | None = None
    sev_mismatch: str | None = None  # up | down | diff | None
    dup: bool = False  # a DUP_OF_TP also names this golden
    confidence: str | None = None
    counted: bool | None = None
    justification: str = ""
    category: str | None = None
    category_reason: str = ""
    missed_where: str = ""
    missed_note: str = ""
    error_stage: str = ""
    error_detail: str = ""
    scored_as: str = ""  # human note where display status ≠ scored outcome

    def __post_init__(self) -> None:
        glyph, label, _ = STATUS_LABELS[self.status]
        self.label = self.label or label
        self.glyph = glyph

    @property
    def is_hit(self) -> bool:
        return self.status in HIT_STATUSES

    @property
    def is_miss(self) -> bool:
        return self.status in MISS_STATUSES


def _pick_match(rows: list[FindingMatch]) -> FindingMatch:
    return sorted(
        rows,
        key=lambda m: (bool(m.counted), CONFIDENCE_RANK.get(m.confidence, 0)),
        reverse=True,
    )[0]


def finding_status(
    golden: Golden,
    cr: CaseResult | None,
    matches: list[FindingMatch],
    classification: dict | None,
) -> CellDetail:
    """Derive the display status of one golden for one case result.

    Precedence (first rule wins): not_assessed → error → not_graded →
    not_in_key → matched branch → missed branch.
    """
    base = dict(golden_id=golden.id, optional=golden.optional, retired=golden.retired,
                expected_description=golden.description, expected_severity=golden.severity)
    if cr is None:
        return CellDetail("not_assessed", **base)
    base.update(case_result_id=cr.id, run_id=cr.run_id, case_id=cr.case_id,
                repetition=cr.repetition)
    if cr.status in ("error", "judge_error"):
        return CellDetail("error", error_stage=cr.error_stage or cr.status,
                          error_detail=cr.error_detail or "", **base)
    if not matches:
        return CellDetail("not_graded", **base)
    rows = [m for m in matches if m.expected_id == golden.id and m.match_type != "extra"]
    if not rows:
        return CellDetail("not_in_key", **base)

    cls = classification or {}
    entries = cls.get("classification") or []
    by_actual = {e.get("id"): e for e in entries if isinstance(e, dict)}
    dup = any(e.get("category") == "DUP_OF_TP" and e.get("golden") == golden.id
              for e in entries if isinstance(e, dict))
    # finding_match stores the key as it was at grading time; prefer it over
    # today's yaml so a re-worded golden shows what the judge actually saw.
    first = rows[0]
    if first.expected_description:
        base["expected_description"] = first.expected_description
    if first.expected_severity:
        base["expected_severity"] = first.expected_severity
    optional = bool(first.expected_optional) or golden.optional
    base["optional"] = optional

    matched = [m for m in rows if m.match_type == "matched"]
    if matched:
        m = _pick_match(matched)
        entry = by_actual.get(m.actual_weakness_id) or {}
        common = dict(
            actual_weakness_id=m.actual_weakness_id, actual_description=m.actual_description,
            actual_severity=m.actual_severity, confidence=m.confidence, counted=bool(m.counted),
            justification=m.justification or "", category=entry.get("category"),
            category_reason=entry.get("reason") or "",
            sev_mismatch=severity_mismatch(base["expected_severity"], m.actual_severity),
            dup=dup,
        )
        if entry.get("category") == "JUDGE_FP_MATCH":
            return CellDetail("false_match", scored_as="hit (TP)", **base, **common)
        if optional:
            return CellDetail("identified_optional", scored_as="not scored", **base, **common)
        if not m.counted:
            return CellDetail("low_confidence", scored_as="miss (FN) + false positive (FP)",
                              **base, **common)
        return CellDetail("identified", scored_as="hit (TP)", **base, **common)

    m = rows[0]  # missed
    common = dict(counted=bool(m.counted), justification=m.justification or "", dup=dup)
    judge_hit = next(
        (e for e in entries if isinstance(e, dict)
         and e.get("category") == "JUDGE_FN" and e.get("golden") == golden.id),
        None,
    )
    if judge_hit:
        return CellDetail(
            "identified_by_judge", scored_as="miss (FN)",
            actual_weakness_id=judge_hit.get("id"), category="JUDGE_FN",
            category_reason=judge_hit.get("reason") or "", **base, **common,
        )
    if optional:
        return CellDetail("optional_missed", scored_as="not scored", **base, **common)
    missed_entry = next(
        (g for g in cls.get("missed_goldens") or []
         if isinstance(g, dict) and g.get("golden") == golden.id),
        None,
    )
    if missed_entry is not None and missed_entry.get("fact_in_chunks") is not None:
        status = "missed_reasoning" if missed_entry.get("fact_in_chunks") else "missed_ingestion"
        return CellDetail(status, scored_as="miss (FN)",
                          missed_where=missed_entry.get("where") or "",
                          missed_note=missed_entry.get("note") or "", **base, **common)
    return CellDetail("missed", scored_as="miss (FN)", **base, **common)


# ---------------------------------------------------------------------------
# DB fetch (column-only where the blobs would hurt)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgeStat:
    case_result_id: int
    purpose: str
    model_id: str
    prompt_version: str
    latency_ms: int | None
    cost_usd: float | None
    cost_source: str
    ok: bool
    error: str


def fetch_results(session: Session, run_ids: Iterable[int] | None = None,
                  with_report: bool = False) -> list[CaseResult]:
    q = select(CaseResult).order_by(CaseResult.run_id, CaseResult.case_id, CaseResult.repetition)
    if run_ids is not None:
        q = q.where(CaseResult.run_id.in_(list(run_ids)))
    if not with_report:
        q = q.options(defer(CaseResult.report_json))
    return list(session.scalars(q).all())


def fetch_matches(session: Session, case_result_ids: Iterable[int]) -> dict[int, list[FindingMatch]]:
    ids = list(case_result_ids)
    out: dict[int, list[FindingMatch]] = defaultdict(list)
    if not ids:
        return out
    for fm in session.scalars(
        select(FindingMatch).where(FindingMatch.case_result_id.in_(ids)).order_by(FindingMatch.id)
    ).all():
        out[fm.case_result_id].append(fm)
    return out


def fetch_judge_stats(session: Session, case_result_ids: Iterable[int]) -> dict[int, list[JudgeStat]]:
    """Judge call metadata without the request/response blobs."""
    ids = list(case_result_ids)
    out: dict[int, list[JudgeStat]] = defaultdict(list)
    if not ids:
        return out
    rows = session.execute(
        select(JudgeCall.case_result_id, JudgeCall.purpose, JudgeCall.model_id,
               JudgeCall.prompt_version, JudgeCall.latency_ms, JudgeCall.cost_usd,
               JudgeCall.cost_source, JudgeCall.ok, JudgeCall.error)
        .where(JudgeCall.case_result_id.in_(ids)).order_by(JudgeCall.id)
    ).all()
    for r in rows:
        out[r.case_result_id].append(JudgeStat(
            r.case_result_id, r.purpose, r.model_id, r.prompt_version, r.latency_ms,
            r.cost_usd, r.cost_source or "", bool(r.ok), r.error or "",
        ))
    return out


def first_seen_assessments(session: Session) -> dict[int, int]:
    """assessment_id → id of the first case result (DB order) that reported
    it. Any later case result re-reports spend already counted there."""
    rows = session.execute(
        select(CaseResult.id, CaseResult.assessment_id)
        .where(CaseResult.assessment_id.is_not(None)).order_by(CaseResult.id)
    ).all()
    first: dict[int, int] = {}
    for cr_id, aid in rows:
        first.setdefault(aid, cr_id)
    return first


# ---------------------------------------------------------------------------
# Per case-result rollup
# ---------------------------------------------------------------------------


@dataclass
class CaseCosts:
    assess: Money
    assess_time_s: float | None
    judge: Money
    judge_time_s: float | None
    reused: bool  # assessment spend already counted for an earlier case result


def case_costs(cr: CaseResult, judge_stats: list[JudgeStat], first_seen: dict[int, int]) -> CaseCosts:
    tokens = parse_json(cr.tokens_json, None)
    money = case_cost(tokens)
    reused = cr.assessment_id is not None and first_seen.get(cr.assessment_id, cr.id) != cr.id
    if reused:
        money = Money(money.usd, money.estimated, money.missing, reused=True)
    return CaseCosts(
        assess=money,
        assess_time_s=case_time_s(parse_json(cr.timings_json, {})),
        judge=judge_cost(judge_stats),
        judge_time_s=judge_time_s(judge_stats),
        reused=reused,
    )


# ---------------------------------------------------------------------------
# Run summary (index rows, run header, matrix columns)
# ---------------------------------------------------------------------------


@dataclass
class VendorRepStatus:
    case_result_id: int
    repetition: int
    status: str  # ok | error | judge_error
    assessment_id: int | None
    recall: float | None
    error_stage: str


@dataclass
class VendorChip:
    case_id: str
    vendor_name: str
    slot: int  # fixed colour slot
    reps: list[VendorRepStatus] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.reps)


@dataclass
class RunSummary:
    id: int
    started_at: str | None
    finished_at: str | None
    status: str
    mode: str
    judge_mode: str
    app_git_sha: str
    full_sha: str
    app_git_dirty: bool
    app_version: str
    model_label: str
    models: list[tuple[str, str]]
    judge_model: str
    judge_prompts: dict
    notes: str
    n_cases: int
    n_ok: int
    n_error: int
    n_reps: int
    mean_f1: float | None
    mean_exec: float | None
    mean_recall: float | None
    mean_precision: float | None
    goldens_identified: int
    goldens_required: int
    vendors: list[VendorChip]
    assess_cost: Money
    assess_time_s: float | None
    judge_cost: Money
    judge_time_s: float | None
    wallclock_s: float | None
    n_reused: int
    reused_assessment_ids: list[int]
    assessment_ids: list[int]

    @property
    def date(self) -> str:
        return (self.started_at or "")[:10]

    @property
    def hit_share(self) -> float | None:
        return self.goldens_identified / self.goldens_required if self.goldens_required else None

    def to_api(self) -> dict:
        return {
            # legacy keys, unchanged
            "id": self.id, "started_at": self.started_at, "status": self.status,
            "app_git_sha": self.app_git_sha, "app_git_dirty": self.app_git_dirty,
            "app_version": self.app_version, "model_label": self.model_label,
            "judge_model": self.judge_model, "notes": self.notes,
            "n_cases": self.n_cases, "n_ok": self.n_ok,
            "mean_f1": self.mean_f1, "mean_exec": self.mean_exec,
            # new
            "mode": self.mode, "judge_mode": self.judge_mode,
            "mean_recall": self.mean_recall, "mean_precision": self.mean_precision,
            "assess_cost_usd": self.assess_cost.usd,
            "assess_cost_estimated": self.assess_cost.estimated,
            "assess_cost_missing": self.assess_cost.missing,
            "assess_time_s": self.assess_time_s,
            "judge_cost_usd": self.judge_cost.usd,
            "judge_cost_estimated": self.judge_cost.estimated,
            "judge_time_s": self.judge_time_s,
            "wallclock_s": self.wallclock_s,
            "goldens_identified": self.goldens_identified,
            "goldens_required": self.goldens_required,
            "n_reused": self.n_reused,
            "vendors": [
                {"case_id": v.case_id, "reps": [
                    {"repetition": r.repetition, "status": r.status,
                     "assessment_id": r.assessment_id, "case_result_id": r.case_result_id}
                    for r in v.reps]}
                for v in self.vendors if v.present
            ],
        }


def vendor_slots(index: dict[str, CaseInfo], extra_ids: Iterable[str] = ()) -> dict[str, int]:
    """Fixed colour slot per vendor: yaml cases in directory order, then any
    historical case ids alphabetically."""
    order = list(index)
    for cid in sorted(set(extra_ids)):
        if cid not in order:
            order.append(cid)
    return {cid: i for i, cid in enumerate(order)}


def build_run_summary(
    run: Run,
    results: list[CaseResult],
    judge_stats: dict[int, list[JudgeStat]],
    first_seen: dict[int, int],
    index: dict[str, CaseInfo],
    slots: dict[str, int],
) -> RunSummary:
    ok = [r for r in results if r.status == "ok"]
    costs = {cr.id: case_costs(cr, judge_stats.get(cr.id, []), first_seen) for cr in results}
    assess = sum((c.assess for c in costs.values()), Money())
    judge = sum((c.judge for c in costs.values()), Money())
    assess_times = [c.assess_time_s for c in costs.values() if c.assess_time_s is not None]
    judge_times = [c.judge_time_s for c in costs.values() if c.judge_time_s is not None]

    by_case: dict[str, list[CaseResult]] = defaultdict(list)
    for cr in results:
        by_case[cr.case_id].append(cr)
    chips = []
    for cid, slot in sorted(slots.items(), key=lambda kv: kv[1]):
        info = index.get(cid)
        chip = VendorChip(cid, info.vendor_name if info else cid, slot)
        for cr in sorted(by_case.get(cid, []), key=lambda c: c.repetition):
            chip.reps.append(VendorRepStatus(cr.id, cr.repetition, cr.status, cr.assessment_id,
                                             cr.recall, cr.error_stage or ""))
        chips.append(chip)

    reused_ids = sorted({cr.assessment_id for cr in results
                         if costs[cr.id].reused and cr.assessment_id is not None})
    return RunSummary(
        id=run.id,
        started_at=_iso(run.started_at),
        finished_at=_iso(run.finished_at),
        status=run.status,
        mode=run_mode(run),
        judge_mode=judge_mode_of(run, results),
        app_git_sha=(run.app_git_sha or "")[:7],
        full_sha=run.app_git_sha or "",
        app_git_dirty=bool(run.app_git_dirty),
        app_version=run.app_version or "",
        model_label=_model_label(run),
        models=model_profiles(run),
        judge_model=run.judge_model or "",
        judge_prompts=parse_json(run.judge_prompt_versions_json, {}) or {},
        notes=run.notes or "",
        n_cases=len(results),
        n_ok=len(ok),
        n_error=len(results) - len(ok),
        n_reps=max((cr.repetition for cr in results), default=0),
        mean_f1=_mean([r.f1 for r in ok]),
        mean_exec=_mean([r.exec_overall for r in ok]),
        mean_recall=_mean([r.recall for r in ok]),
        mean_precision=_mean([r.precision for r in ok]),
        goldens_identified=sum(r.tp or 0 for r in ok),
        goldens_required=sum((r.tp or 0) + (r.fn or 0) for r in ok),
        vendors=chips,
        assess_cost=assess,
        assess_time_s=sum(assess_times) if assess_times else None,
        judge_cost=judge,
        judge_time_s=sum(judge_times) if judge_times else None,
        wallclock_s=wallclock_s(run),
        n_reused=sum(1 for c in costs.values() if c.reused),
        reused_assessment_ids=reused_ids,
        assessment_ids=sorted({cr.assessment_id for cr in results if cr.assessment_id is not None}),
    )


# ---------------------------------------------------------------------------
# Totals (reuse-aware)
# ---------------------------------------------------------------------------


@dataclass
class Totals:
    n_runs: int
    n_assessments: int  # distinct assessment ids — same figure as `bench cost`
    n_unattributed: int  # case results with no assessment id (failed before create)
    assess_cost: Money
    assess_time_s: float | None
    judge_cost: Money
    judge_time_s: float | None
    n_judge_calls: int

    @property
    def overall_cost(self) -> Money:
        return self.assess_cost + self.judge_cost

    @property
    def overall_time_s(self) -> float | None:
        parts = [t for t in (self.assess_time_s, self.judge_time_s) if t is not None]
        return sum(parts) if parts else None


def build_totals(results: list[CaseResult], judge_stats: dict[int, list[JudgeStat]],
                 first_seen: dict[int, int], n_runs: int) -> Totals:
    """Assessment spend counted once per assessment; judge summed over every call."""
    ledger = AssessmentLedger()
    assess_times: list[float] = []
    judge = Money()
    judge_times: list[float] = []
    n_calls = 0
    # Reuse is resolved *within the given results* (DB order): when a grade
    # run is compared without the run that produced its assessments, their
    # spend is still counted once here.
    for cr in sorted(results, key=lambda c: c.id):
        c = case_costs(cr, judge_stats.get(cr.id, []), first_seen)
        assess = Money(c.assess.usd, c.assess.estimated, c.assess.missing)
        if not ledger.record(cr.assessment_id, assess) and c.assess_time_s is not None:
            assess_times.append(c.assess_time_s)
        judge += c.judge
        if c.judge_time_s is not None:
            judge_times.append(c.judge_time_s)
        n_calls += len(judge_stats.get(cr.id, []))
    return Totals(
        n_runs=n_runs,
        n_assessments=ledger.distinct,
        n_unattributed=len(ledger.unattributed),
        assess_cost=ledger.total(),
        assess_time_s=sum(assess_times) if assess_times else None,
        judge_cost=judge,
        judge_time_s=sum(judge_times) if judge_times else None,
        n_judge_calls=n_calls,
    )


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


@dataclass
class IndexView:
    summaries: list[RunSummary]  # newest first, visible only
    hidden: int
    include_empty: bool
    totals: Totals
    trend: dict
    vendors: list[tuple[str, str, int]]  # (case_id, vendor_name, slot)
    default_compare: list[int]


def _load_everything(session: Session, run_ids: Iterable[int] | None = None):
    runs = list(session.scalars(select(Run).order_by(Run.id)).all())
    if run_ids is not None:
        wanted = set(run_ids)
        runs = [r for r in runs if r.id in wanted]
    results = fetch_results(session, [r.id for r in runs] if run_ids is not None else None)
    judge_stats = fetch_judge_stats(session, [cr.id for cr in results])
    first_seen = first_seen_assessments(session)
    index = load_golden_index()
    slots = vendor_slots(index, (cr.case_id for cr in results))
    return runs, results, judge_stats, first_seen, index, slots


def build_index(session: Session, include_empty: bool = False) -> IndexView:
    runs, results, judge_stats, first_seen, index, slots = _load_everything(session)
    by_run: dict[int, list[CaseResult]] = defaultdict(list)
    for cr in results:
        by_run[cr.run_id].append(cr)
    all_summaries = [
        build_run_summary(r, by_run.get(r.id, []), judge_stats, first_seen, index, slots)
        for r in runs
    ]
    visible = [s for s in all_summaries if include_empty or s.n_ok > 0]
    hidden = len(all_summaries) - len(visible)
    vendors = [(cid, (index[cid].vendor_name if cid in index else cid), slot)
               for cid, slot in sorted(slots.items(), key=lambda kv: kv[1])]
    trend = build_trend(visible, by_run, vendors)
    totals = build_totals(results, judge_stats, first_seen, len(runs))
    graded = [s.id for s in all_summaries if s.n_ok > 0]
    return IndexView(
        summaries=list(reversed(visible)),
        hidden=hidden,
        include_empty=include_empty,
        totals=totals,
        trend=trend,
        vendors=vendors,
        default_compare=graded[-3:],
    )


def build_trend(summaries: list[RunSummary], by_run: dict[int, list[CaseResult]],
                vendors: list[tuple[str, str, int]]) -> dict:
    """Per-vendor series (mean over ok reps) across the visible runs, oldest first."""
    trend: dict = {"labels": [], "runs": [], "recall": {}, "f1": {}, "exec": {}, "vendors": []}
    for s in summaries:
        trend["labels"].append(f"#{s.id} · {s.date[5:]}")
        trend["runs"].append({"id": s.id, "sha": s.app_git_sha, "mode": s.mode})
    for cid, name, slot in vendors:
        rec, f1, ex = [], [], []
        for s in summaries:
            ok = [cr for cr in by_run.get(s.id, []) if cr.case_id == cid and cr.status == "ok"]
            rec.append(_mean([cr.recall for cr in ok]))
            f1.append(_mean([cr.f1 for cr in ok]))
            ex.append(_mean([cr.exec_overall for cr in ok]))
        if any(v is not None for v in rec + f1 + ex):
            trend["vendors"].append({"case_id": cid, "name": name, "slot": slot})
            trend["recall"][cid] = rec
            trend["f1"][cid] = f1
            trend["exec"][cid] = ex
    return trend


# ---------------------------------------------------------------------------
# Compare matrix
# ---------------------------------------------------------------------------


@dataclass
class MatrixSubColumn:
    run_id: int
    repetition: int
    key: str  # "18:1"


@dataclass
class MatrixColumn:
    run: RunSummary
    reps: list[MatrixSubColumn]


@dataclass
class ExtraCounts:
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def segments(self) -> list[tuple[str, str, int, float]]:
        """(category, label, n, share) for a stacked bar."""
        tot = self.total or 1
        return [(c, EXTRA_LABELS[c], n, n / tot) for c in EXTRA_CATEGORIES
                if (n := self.counts.get(c, 0))]


@dataclass
class VendorRunSummary:
    case_result_id: int
    run_id: int
    repetition: int
    status: str
    assessment_id: int | None
    assessment_deleted: bool
    band: str | None
    expected_band: str | None
    band_error: int | None
    recall: float | None
    precision: float | None
    f1: float | None
    tp: int | None
    fp: int | None
    fn: int | None
    n_weaknesses: int | None
    signal_share: float | None
    exec_overall: float | None
    extras: ExtraCounts
    costs: CaseCosts
    error_stage: str
    error_detail: str
    hits: int
    goldens_total: int


@dataclass
class MatrixRow:
    golden: Golden
    cells: list[CellDetail]  # aligned with the flattened sub-columns
    differs: bool


@dataclass
class MatrixGroup:
    case_id: str
    vendor_name: str
    expected_band: str | None
    slot: int
    load_error: str | None
    rows: list[MatrixRow]
    retired_rows: list[MatrixRow]
    summaries: list[VendorRunSummary | None]  # aligned with the flattened sub-columns
    hit_counts: list[tuple[int, int] | None]  # (hits, required) per sub-column


@dataclass
class Matrix:
    columns: list[MatrixColumn]
    subcolumns: list[MatrixSubColumn]
    groups: list[MatrixGroup]
    totals: Totals
    run_ids: list[int]
    vendor_filter: list[str]
    all_vendors: list[tuple[str, str, int]]


def vendor_run_summary(cr: CaseResult, info: CaseInfo, cells: list[CellDetail],
                       costs: CaseCosts) -> VendorRunSummary:
    cls = parse_json(cr.classification_json, {}) or {}
    # Count the classifier's per-finding verdicts directly (what the case page
    # lists); fall back to its `counts` summary only when entries are absent.
    entries = [e for e in (cls.get("classification") or []) if isinstance(e, dict)]
    counts: dict[str, int] = defaultdict(int)
    for e in entries:
        if e.get("category") in EXTRA_CATEGORIES:
            counts[e["category"]] += 1
    counts = dict(counts)
    if not entries:
        counts = {k: int(v) for k, v in (cls.get("counts") or {}).items() if k in EXTRA_CATEGORIES}
        if cls.get("judge_fp_match") and "JUDGE_FP_MATCH" not in counts:
            counts["JUDGE_FP_MATCH"] = int(cls["judge_fp_match"])
    required = [c for c in cells if not c.optional and c.status not in
                ("not_in_key", "not_graded", "error", "not_assessed")]
    return VendorRunSummary(
        case_result_id=cr.id, run_id=cr.run_id, repetition=cr.repetition, status=cr.status,
        assessment_id=cr.assessment_id, assessment_deleted=bool(cr.assessment_deleted),
        band=cr.aggregate_band or None, expected_band=info.expected_band,
        band_error=cr.band_error if cr.band_error is not None
        else band_error_of(cr.aggregate_band, info.expected_band),
        recall=cr.recall, precision=cr.precision, f1=cr.f1, tp=cr.tp, fp=cr.fp, fn=cr.fn,
        n_weaknesses=cr.n_weaknesses, signal_share=cr.signal_share, exec_overall=cr.exec_overall,
        extras=ExtraCounts(counts), costs=costs,
        error_stage=cr.error_stage or "", error_detail=cr.error_detail or "",
        hits=sum(1 for c in required if c.status in ("identified", "false_match")),
        goldens_total=len(required),
    )


def build_matrix(session: Session, run_ids: list[int], vendors: list[str] | None = None) -> Matrix:
    runs, results, judge_stats, first_seen, index, slots = _load_everything(session, run_ids)
    by_id = {r.id: r for r in runs}
    ordered_runs = [by_id[i] for i in run_ids]
    by_run: dict[int, list[CaseResult]] = defaultdict(list)
    for cr in results:
        by_run[cr.run_id].append(cr)
    matches = fetch_matches(session, [cr.id for cr in results])

    columns: list[MatrixColumn] = []
    subcolumns: list[MatrixSubColumn] = []
    summaries: dict[int, RunSummary] = {}
    for run in ordered_runs:
        s = build_run_summary(run, by_run.get(run.id, []), judge_stats, first_seen, index, slots)
        summaries[run.id] = s
        reps = sorted({cr.repetition for cr in by_run.get(run.id, [])}) or [1]
        subs = [MatrixSubColumn(run.id, rep, f"{run.id}:{rep}") for rep in reps]
        columns.append(MatrixColumn(s, subs))
        subcolumns.extend(subs)

    matches_by_case: dict[str, list[FindingMatch]] = defaultdict(list)
    for cr in results:
        matches_by_case[cr.case_id].extend(matches.get(cr.id, []))
    keyed = with_legacy_goldens(index, matches_by_case)

    case_ids = sorted({cr.case_id for cr in results}, key=lambda c: slots.get(c, 999))
    vendor_filter = [v for v in (vendors or []) if v in case_ids]
    if vendor_filter:
        case_ids = [c for c in case_ids if c in vendor_filter]

    lookup = {(cr.run_id, cr.case_id, cr.repetition): cr for cr in results}
    groups: list[MatrixGroup] = []
    for cid in case_ids:
        info = keyed.get(cid) or CaseInfo(cid, cid, None, [])
        crs = [lookup.get((sc.run_id, cid, sc.repetition)) for sc in subcolumns]
        cells_by_col: list[list[CellDetail]] = []
        for cr in crs:
            cls = parse_json(cr.classification_json, None) if cr else None
            fms = matches.get(cr.id, []) if cr else []
            cells_by_col.append([finding_status(g, cr, fms, cls) for g in info.goldens])
        rows, retired = [], []
        for gi, g in enumerate(info.goldens):
            cells = [col[gi] for col in cells_by_col]
            statuses = {c.status for c in cells if c.status not in NEUTRAL_STATUSES}
            row = MatrixRow(g, cells, differs=len(statuses) > 1)
            (retired if g.retired else rows).append(row)
        col_summaries: list[VendorRunSummary | None] = []
        hit_counts: list[tuple[int, int] | None] = []
        for cr, cells in zip(crs, cells_by_col):
            if cr is None:
                col_summaries.append(None)
                hit_counts.append(None)
                continue
            vs = vendor_run_summary(cr, info, cells,
                                    case_costs(cr, judge_stats.get(cr.id, []), first_seen))
            col_summaries.append(vs)
            hit_counts.append((vs.hits, vs.goldens_total) if cr.status == "ok" else None)
        groups.append(MatrixGroup(
            case_id=cid, vendor_name=info.vendor_name, expected_band=info.expected_band,
            slot=slots.get(cid, 0), load_error=info.load_error, rows=rows, retired_rows=retired,
            summaries=col_summaries, hit_counts=hit_counts,
        ))

    totals = build_totals(results, judge_stats, first_seen, len(ordered_runs))
    all_vendors = [(cid, keyed[cid].vendor_name if cid in keyed else cid, slots.get(cid, 0))
                   for cid in sorted({cr.case_id for cr in results}, key=lambda c: slots.get(c, 999))]
    return Matrix(columns, subcolumns, groups, totals, list(run_ids), vendor_filter, all_vendors)


def matrix_to_api(m: Matrix) -> dict:
    def cell(c: CellDetail) -> dict:
        d = asdict(c)
        d.pop("expected_description", None)
        return d

    return {
        "runs": [c.run.to_api() for c in m.columns],
        "columns": [asdict(sc) for sc in m.subcolumns],
        "vendors": [
            {
                "case_id": g.case_id, "vendor_name": g.vendor_name,
                "expected_band": g.expected_band,
                "goldens": [
                    {"id": r.golden.id, "severity": r.golden.severity,
                     "optional": r.golden.optional, "retired": r.golden.retired,
                     "differs": r.differs, "cells": [cell(c) for c in r.cells]}
                    for r in g.rows + g.retired_rows
                ],
                "summaries": [
                    None if s is None else {
                        "case_result_id": s.case_result_id, "repetition": s.repetition,
                        "status": s.status, "assessment_id": s.assessment_id,
                        "band": s.band, "expected_band": s.expected_band,
                        "band_error": s.band_error, "recall": s.recall,
                        "precision": s.precision, "f1": s.f1, "n_weaknesses": s.n_weaknesses,
                        "signal_share": s.signal_share, "extras": s.extras.counts,
                        "assess_cost_usd": s.costs.assess.usd,
                        "assess_cost_estimated": s.costs.assess.estimated,
                        "assess_cost_reused": s.costs.reused,
                        "assess_time_s": s.costs.assess_time_s,
                        "judge_cost_usd": s.costs.judge.usd,
                        "judge_time_s": s.costs.judge_time_s,
                    }
                    for s in g.summaries
                ],
            }
            for g in m.groups
        ],
        "totals": {
            "n_assessments": m.totals.n_assessments,
            "assess_cost_usd": m.totals.assess_cost.usd,
            "assess_time_s": m.totals.assess_time_s,
            "judge_cost_usd": m.totals.judge_cost.usd,
            "judge_time_s": m.totals.judge_time_s,
        },
    }


# ---------------------------------------------------------------------------
# Run detail
# ---------------------------------------------------------------------------


@dataclass
class StageBar:
    name: str
    seconds: float
    share: float


@dataclass
class PurposeCost:
    purpose: str
    model_id: str
    calls: int
    errors: int
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost: Money
    share: float


@dataclass
class RepDetail:
    summary: VendorRunSummary
    cells: list[CellDetail]
    retired_cells: list[CellDetail]
    stages: list[StageBar]
    purposes: list[PurposeCost]
    judge_calls: list[JudgeStat]
    tokens: dict | None
    timings: dict


@dataclass
class VendorDetail:
    case_id: str
    vendor_name: str
    expected_band: str | None
    slot: int
    load_error: str | None
    reps: list[RepDetail]


@dataclass
class RunDetail:
    summary: RunSummary
    config: dict
    models_json: dict
    backend_url: str
    vendors: list[VendorDetail]
    prev_id: int | None
    next_id: int | None
    charts: dict


def stage_bars(timings: dict) -> list[StageBar]:
    total = sum(v for v in timings.values() if v) or 0.0
    names = [s for s in STAGE_ORDER if s in timings] + [s for s in timings if s not in STAGE_ORDER]
    return [StageBar(n, float(timings[n] or 0), (float(timings[n] or 0) / total) if total else 0.0)
            for n in names]


def purpose_costs(tokens: dict | None) -> list[PurposeCost]:
    if not tokens:
        return []
    rows = tokens.get("by_purpose") or []
    total = sum((p.get("cost_usd") or 0.0) for p in rows) or 0.0
    out = []
    for p in sorted(rows, key=lambda p: -(p.get("cost_usd") or 0.0)):
        usd = p.get("cost_usd")
        out.append(PurposeCost(
            purpose=p.get("purpose", "?"), model_id=p.get("model_id", "?"),
            calls=int(p.get("calls") or 0), errors=int(p.get("errors") or 0),
            input_tokens=int(p.get("input_tokens") or 0),
            output_tokens=int(p.get("output_tokens") or 0),
            latency_s=(p.get("latency_ms") or 0) / 1000.0,
            cost=Money(None if usd is None else float(usd), bool(p.get("cost_estimated")),
                       int(p.get("cost_missing") or 0)),
            share=((usd or 0.0) / total) if total else 0.0,
        ))
    return out


def _rep_detail(cr: CaseResult, info: CaseInfo, fms: list[FindingMatch],
                stats: list[JudgeStat], first_seen: dict[int, int]) -> RepDetail:
    cls = parse_json(cr.classification_json, None)
    cells = [finding_status(g, cr, fms, cls) for g in info.goldens]
    costs = case_costs(cr, stats, first_seen)
    tokens = parse_json(cr.tokens_json, None)
    timings = parse_json(cr.timings_json, {}) or {}
    return RepDetail(
        summary=vendor_run_summary(cr, info, cells, costs),
        cells=[c for c in cells if not c.retired],
        retired_cells=[c for c in cells if c.retired],
        stages=stage_bars(timings),
        purposes=purpose_costs(tokens),
        judge_calls=stats,
        tokens=tokens,
        timings=timings,
    )


def build_run_detail(session: Session, run_id: int) -> RunDetail | None:
    run = session.get(Run, run_id)
    if run is None:
        return None
    results = fetch_results(session, [run_id])
    judge_stats = fetch_judge_stats(session, [cr.id for cr in results])
    first_seen = first_seen_assessments(session)
    index = load_golden_index()
    slots = vendor_slots(index, (cr.case_id for cr in results))
    matches = fetch_matches(session, [cr.id for cr in results])
    summary = build_run_summary(run, results, judge_stats, first_seen, index, slots)

    matches_by_case: dict[str, list[FindingMatch]] = defaultdict(list)
    for cr in results:
        matches_by_case[cr.case_id].extend(matches.get(cr.id, []))
    keyed = with_legacy_goldens(index, matches_by_case)

    by_case: dict[str, list[CaseResult]] = defaultdict(list)
    for cr in results:
        by_case[cr.case_id].append(cr)
    vendors = []
    for cid in sorted(by_case, key=lambda c: slots.get(c, 999)):
        info = keyed.get(cid) or CaseInfo(cid, cid, None, [])
        reps = [
            _rep_detail(cr, info, matches.get(cr.id, []), judge_stats.get(cr.id, []), first_seen)
            for cr in sorted(by_case[cid], key=lambda c: c.repetition)
        ]
        vendors.append(VendorDetail(cid, info.vendor_name, info.expected_band,
                                    slots.get(cid, 0), info.load_error, reps))

    ids = list(session.scalars(select(Run.id).order_by(Run.id)).all())
    pos = ids.index(run_id)
    ok_rows = [cr for cr in results if cr.status == "ok"]
    charts = {
        "labels": [f"{cr.case_id} r{cr.repetition}" for cr in ok_rows],
        "slots": [slots.get(cr.case_id, 0) for cr in ok_rows],
        "precision": [cr.precision for cr in ok_rows],
        "recall": [cr.recall for cr in ok_rows],
        "f1": [cr.f1 for cr in ok_rows],
        "exec_coverage": [cr.exec_coverage for cr in ok_rows],
        "exec_faithfulness": [cr.exec_faithfulness for cr in ok_rows],
        "exec_violation": [cr.exec_violation for cr in ok_rows],
    }
    return RunDetail(
        summary=summary,
        config=parse_json(run.config_json, {}) or {},
        models_json=parse_json(run.models_json, {}) or {},
        backend_url=run.backend_url or "",
        vendors=vendors,
        prev_id=ids[pos - 1] if pos > 0 else None,
        next_id=ids[pos + 1] if pos + 1 < len(ids) else None,
        charts=charts,
    )


# ---------------------------------------------------------------------------
# Case detail
# ---------------------------------------------------------------------------


@dataclass
class ExtraFinding:
    match: FindingMatch
    category: str | None
    reason: str
    golden: str | None


@dataclass
class CaseDetail:
    cr: CaseResult
    run: RunSummary
    vendor_name: str
    expected_band: str | None
    slot: int
    rep: RepDetail
    extras: list[tuple[str, str, list[ExtraFinding]]]  # (category, label, findings)
    classification: dict | None
    rubric: dict | None
    exec_summary: dict | None
    judge_calls: list[JudgeCall]


def build_case_detail(session: Session, run_id: int, case_result_id: int) -> CaseDetail | None:
    cr = session.get(CaseResult, case_result_id)
    if cr is None or cr.run_id != run_id:
        return None
    run = session.get(Run, run_id)
    results = fetch_results(session, [run_id])
    judge_stats = fetch_judge_stats(session, [c.id for c in results])
    first_seen = first_seen_assessments(session)
    index = load_golden_index()
    slots = vendor_slots(index, (c.case_id for c in results))
    summary = build_run_summary(run, results, judge_stats, first_seen, index, slots)

    fms = fetch_matches(session, [cr.id]).get(cr.id, [])
    keyed = with_legacy_goldens(index, {cr.case_id: fms})
    info = keyed.get(cr.case_id) or CaseInfo(cr.case_id, cr.case_id, None, [])
    rep = _rep_detail(cr, info, fms, judge_stats.get(cr.id, []), first_seen)

    classification = parse_json(cr.classification_json, None)
    by_actual = {c.get("id"): c for c in (classification or {}).get("classification", [])
                 if isinstance(c, dict)}
    grouped: dict[str, list[ExtraFinding]] = defaultdict(list)
    for m in fms:
        if m.match_type != "extra":
            continue
        entry = by_actual.get(m.actual_weakness_id) or {}
        cat = entry.get("category") or ("unclassified" if classification else "extra")
        grouped[cat].append(ExtraFinding(m, entry.get("category"), entry.get("reason") or "",
                                         entry.get("golden")))
    order = list(EXTRA_CATEGORIES) + sorted(k for k in grouped if k not in EXTRA_CATEGORIES)
    extras = [(k, EXTRA_LABELS.get(k, k.replace("_", " ").lower()), grouped[k])
              for k in order if grouped.get(k)]

    judge_calls = list(session.scalars(
        select(JudgeCall).where(JudgeCall.case_result_id == cr.id).order_by(JudgeCall.id)
    ).all())
    rubric = None
    for jc in judge_calls:
        if jc.purpose == "exec_rubric" and jc.ok:
            rubric = parse_json(jc.response_json, None)
    report = parse_json(cr.report_json, {}) or {}
    return CaseDetail(
        cr=cr, run=summary, vendor_name=info.vendor_name, expected_band=info.expected_band,
        slot=slots.get(cr.case_id, 0), rep=rep, extras=extras, classification=classification,
        rubric=rubric, exec_summary=report.get("executive_summary"), judge_calls=judge_calls,
    )
