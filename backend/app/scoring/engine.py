"""Deterministic risk scoring (accuracy program Phase 5 — R5/R6).

Design: weaknesses change the *state* of the controls they hit instead of
adding per-row uplift; assessment-quality issues (meta) report a confidence
band instead of raising likelihood; residual never exceeds inherent unless an
independently evidenced (auditor-tested) high/critical deficiency maps to the
scenario.

Pipeline (per scenario):
  1. Each expected control's (coverage, effectiveness) is adjusted by the
     weaknesses mapped to it — an operating exception, not arithmetic:
       high/critical weakness → effectiveness forced to "weak"
       medium weakness        → effectiveness capped at "adequate"
       low                    → no state change
     (Adjustments are auditable via `state_downgrades`.)
  2. effectiveness_score(coverage, effectiveness) ∈ [0, 1], weighted average
     → coverage_index; likelihood_reduction = round(coverage_index × 3).
  3. residual_likelihood = clamp(inherent − reduction + uplift) where uplift
     is 0 or 1: +1 only when ≥ 1 critical or ≥ 2 distinct high/critical
     deficiencies map to the scenario. Residual is capped at inherent unless
     at least one of those deficiencies is auditor-tested (evidence_strength
     "auditor_tested"), in which case it may exceed inherent by at most one.
  4. band = lookup_4x4(residual_impact, residual_likelihood).
  5. confidence (high/medium/low) from the scenario's meta issues — reported,
     never scored.

Aggregation: the impact-weighted mean of scenario band ranks drives the
overall band; VeryHigh additionally requires either the weighted mean to
round there or ≥ 2 independent scenarios at VeryHigh. Confidence = the worst
scenario confidence.

No LLM is invoked here. Recalc is sub-millisecond.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.scoring.tables import BAND_RANK, RANK_BAND, RISK_MATRIX

LEVELS = (1, 2, 3, 4)
LEVEL_NAMES = ("Low", "Moderate", "High", "VeryHigh")

_NAME_TO_LEVEL = {n: i + 1 for i, n in enumerate(LEVEL_NAMES)}

# coverage × effectiveness → 0..1 multiplier on inherent likelihood reduction
_EFFECTIVENESS_TABLE: dict[tuple[str, str], float] = {
    ("none", "weak"): 0.0,
    ("none", "adequate"): 0.0,
    ("none", "strong"): 0.0,
    ("none", "unknown"): 0.0,
    ("partial", "weak"): 0.25,
    ("partial", "adequate"): 0.45,
    ("partial", "strong"): 0.6,
    ("partial", "unknown"): 0.25,
    ("full", "weak"): 0.5,
    ("full", "adequate"): 0.8,
    ("full", "strong"): 1.0,
    ("full", "unknown"): 0.45,
}

# Meta issues → reported confidence band (never likelihood). Weights follow
# the old uplift weights so the ordering of "how compromised is this
# assessment" is unchanged — only its effect moved from the score to a label.
_META_WEIGHTS: dict[str, float] = {
    "insufficient_info": 1.0,
    "vague_answer": 0.5,
    "missing_doc": 0.75,
    "conflicting_evidence": 1.0,  # legacy rows only
}
_CONFIDENCE_MEDIUM_AT = 0.5   # any real evidence-quality signal → at most medium
_CONFIDENCE_LOW_AT = 2.0      # several / severe signals → low

_HIGH_SEVERITY = {"high", "critical"}
EVIDENCE_STRENGTHS = ("auditor_tested", "vendor_admitted", "inferred_absence")


def level_to_name(level: int) -> str:
    if not 1 <= level <= 4:
        raise ValueError(f"Level out of range: {level}")
    return LEVEL_NAMES[level - 1]


def level_from_name(name: str) -> int:
    if name not in _NAME_TO_LEVEL:
        raise ValueError(f"Unknown level name: {name}")
    return _NAME_TO_LEVEL[name]


def effectiveness_score(coverage: str, effectiveness: str) -> float:
    return _EFFECTIVENESS_TABLE.get((coverage, effectiveness), 0.3)


def band_for(impact: int, likelihood: int) -> str:
    i = max(1, min(4, impact))
    l = max(1, min(4, likelihood))
    return RISK_MATRIX[i - 1][l - 1]


@dataclass
class ControlInput:
    code: str
    name: str
    weight: float
    coverage: str  # none|partial|full
    effectiveness: str  # weak|adequate|strong|unknown


@dataclass
class MetaIssueInput:
    kind: str  # insufficient_info|vague_answer|missing_doc (+legacy conflicting_evidence)


@dataclass
class WeaknessInput:
    """A weakness mapped to one or more of the scenario's expected controls."""

    severity: str  # low|medium|high|critical
    mapped_control_codes: list[str] = field(default_factory=list)
    # auditor_tested (SOC/ISO/pen-test evidence) > vendor_admitted (the
    # vendor's own statements) > inferred_absence (something not found in a
    # document). Gates whether residual may exceed inherent.
    evidence_strength: str = "vendor_admitted"


@dataclass
class ScenarioInput:
    code: str
    inherent_impact: int  # 1..4
    inherent_likelihood: int  # 1..4
    controls: list[ControlInput] = field(default_factory=list)
    meta_issues: list[MetaIssueInput] = field(default_factory=list)
    weaknesses: list[WeaknessInput] = field(default_factory=list)


@dataclass
class ScenarioScore:
    code: str
    residual_impact: int
    residual_likelihood: int
    band: str
    coverage_index: float  # weighted avg eff_score AFTER state adjustment, in [0, 1]
    likelihood_reduction: int
    uplift: int  # 0 or 1 band (bounded, deficiency-gated)
    distinct_high_critical: int
    auditor_tested_high_critical: int
    confidence: str  # high|medium|low — evidence-quality label, not a score input
    state_downgrades: list[str]  # "CODE: strong→weak (critical, auditor_tested)"
    rationale_breakdown: dict


def _confidence_value(meta: Iterable[MetaIssueInput]) -> tuple[str, float]:
    raw = 0.0
    seen_vague = 0
    for m in meta:
        if m.kind == "vague_answer":
            seen_vague += 1
            if seen_vague > 2:
                continue
        raw += _META_WEIGHTS.get(m.kind, 0.5)
    if raw >= _CONFIDENCE_LOW_AT:
        return "low", raw
    if raw >= _CONFIDENCE_MEDIUM_AT:
        return "medium", raw
    return "high", raw


_EFF_ORDER = {"weak": 0, "unknown": 1, "adequate": 2, "strong": 3}


def _adjusted_effectiveness(eff: str, worst_hit: str | None) -> str:
    """Apply the operating-exception state change for the worst weakness
    severity mapped to this control."""
    if worst_hit in _HIGH_SEVERITY:
        return "weak"
    if worst_hit == "medium" and _EFF_ORDER.get(eff, 1) > _EFF_ORDER["adequate"]:
        return "adequate"
    return eff


def score_scenario(s: ScenarioInput) -> ScenarioScore:
    # Worst mapped severity per control code (state change, not arithmetic).
    sev_rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    worst_by_code: dict[str, str] = {}
    for w in s.weaknesses:
        for c in w.mapped_control_codes or []:
            if not c:
                continue
            if sev_rank.get(w.severity, 0) > sev_rank.get(worst_by_code.get(c, ""), -1):
                worst_by_code[c] = w.severity

    downgrades: list[str] = []
    if not s.controls:
        coverage_index = 0.0
    else:
        total_w = 0.0
        weighted_sum = 0.0
        for c in s.controls:
            eff = _adjusted_effectiveness(c.effectiveness, worst_by_code.get(c.code))
            if eff != c.effectiveness:
                downgrades.append(f"{c.code}: {c.effectiveness}→{eff} ({worst_by_code.get(c.code)})")
            w = max(c.weight, 0.0)
            total_w += w
            weighted_sum += effectiveness_score(c.coverage, eff) * w
        coverage_index = (weighted_sum / total_w) if total_w > 0 else 0.0

    likelihood_reduction = round(coverage_index * 3)

    # Bounded uplift: distinct high/critical deficiencies, not row arithmetic.
    hi = [w for w in s.weaknesses if w.severity in _HIGH_SEVERITY and w.mapped_control_codes]
    n_hi = len(hi)
    n_crit = sum(1 for w in hi if w.severity == "critical")
    n_auditor = sum(1 for w in hi if w.evidence_strength == "auditor_tested")
    uplift = 1 if (n_crit >= 1 or n_hi >= 2) else 0

    inherent_l = max(1, min(4, s.inherent_likelihood))
    inherent_i = max(1, min(4, s.inherent_impact))

    residual_l = inherent_l - likelihood_reduction + uplift
    # Residual ≤ inherent unless an independently evidenced (auditor-tested)
    # high/critical failure maps to this scenario — then at most inherent + 1.
    ceiling = inherent_l + 1 if n_auditor >= 1 else inherent_l
    residual_l = max(1, min(4, min(residual_l, ceiling)))
    residual_i = inherent_i  # impact does not reduce; controls reduce likelihood
    band = band_for(residual_i, residual_l)

    confidence, meta_raw = _confidence_value(s.meta_issues)

    return ScenarioScore(
        code=s.code,
        residual_impact=residual_i,
        residual_likelihood=residual_l,
        band=band,
        coverage_index=round(coverage_index, 3),
        likelihood_reduction=likelihood_reduction,
        uplift=uplift,
        distinct_high_critical=n_hi,
        auditor_tested_high_critical=n_auditor,
        confidence=confidence,
        state_downgrades=downgrades,
        rationale_breakdown={
            "inherent_impact": inherent_i,
            "inherent_likelihood": inherent_l,
            "coverage_index": round(coverage_index, 3),
            "likelihood_reduction_bands": likelihood_reduction,
            "uplift_bands": uplift,
            "distinct_high_critical": n_hi,
            "critical": n_crit,
            "auditor_tested_high_critical": n_auditor,
            "residual_ceiling": ceiling,
            "confidence": confidence,
            "meta_raw": round(meta_raw, 3),
            "state_downgrades": list(downgrades),
        },
    )


_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class AggregateScore:
    band: str
    rank: int
    weighted_mean_rank: float
    top2_mean_rank: float
    confidence: str
    per_scenario: list[ScenarioScore]


def aggregate(scenarios: list[ScenarioScore], inherent_impacts: dict[str, int]) -> AggregateScore:
    if not scenarios:
        return AggregateScore(
            band="Low", rank=1, weighted_mean_rank=1.0, top2_mean_rank=1.0,
            confidence="high", per_scenario=[],
        )

    ranks = sorted((BAND_RANK[s.band] for s in scenarios), reverse=True)
    top2 = ranks[: min(2, len(ranks))]
    top2_mean = sum(top2) / len(top2)  # reported, no longer drives the band

    total_w = 0.0
    weighted_sum = 0.0
    for s in scenarios:
        w = float(inherent_impacts.get(s.code, s.residual_impact))
        total_w += w
        weighted_sum += BAND_RANK[s.band] * w
    weighted_mean = weighted_sum / total_w if total_w > 0 else float(ranks[0])

    # The weighted view drives the band. VeryHigh needs either the weighted
    # mean to round there or at least two independent scenarios at VeryHigh.
    # Half-up rounding, not Python's banker's rounding: a mean sitting exactly
    # on a band boundary resolves conservatively upward (2.5 → High).
    rounded = int(weighted_mean + 0.5)
    rank = max(1, min(4, rounded))
    n_veryhigh = sum(1 for r in ranks if r == 4)
    if rank >= 4 and n_veryhigh < 2 and rounded < 4:
        rank = 3
    if n_veryhigh >= 2:
        rank = 4
    band = RANK_BAND[rank]

    confidence = min((s.confidence for s in scenarios), key=lambda c: _CONF_ORDER[c])

    return AggregateScore(
        band=band,
        rank=rank,
        weighted_mean_rank=round(weighted_mean, 2),
        top2_mean_rank=round(top2_mean, 2),
        confidence=confidence,
        per_scenario=scenarios,
    )
