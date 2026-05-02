"""Deterministic risk scoring.

Pipeline (per scenario):
  1. effectiveness_score(coverage, effectiveness) ∈ [0, 1] for each expected control
  2. weighted average across expected controls
  3. likelihood_reduction = round(weighted_avg × 3) — best case drops 3 bands
  4. residual_likelihood = clamp(inherent − reduction + meta_uplift, 1, 4)
  5. band = lookup_4x4(residual_impact, residual_likelihood)

Aggregation (across scenarios):
  - top-2 average and weighted-mean (by inherent_impact); the UI shows both
  - overall band = the higher of the two (= more conservative)

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

_META_WEIGHTS: dict[str, float] = {
    "insufficient_info": 1.0,
    "vague_answer": 0.5,
    "missing_doc": 0.75,
    "conflicting_evidence": 1.0,
}
_META_UPLIFT_CAP = 2.0


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
    kind: str  # insufficient_info|vague_answer|missing_doc|conflicting_evidence


@dataclass
class ScenarioInput:
    code: str
    inherent_impact: int  # 1..4
    inherent_likelihood: int  # 1..4
    controls: list[ControlInput] = field(default_factory=list)
    meta_issues: list[MetaIssueInput] = field(default_factory=list)


@dataclass
class ScenarioScore:
    code: str
    residual_impact: int
    residual_likelihood: int
    band: str
    coverage_index: float  # weighted avg eff_score, in [0, 1]
    likelihood_reduction: int
    meta_uplift: int
    rationale_breakdown: dict


def _meta_uplift_value(meta: Iterable[MetaIssueInput]) -> float:
    raw = 0.0
    seen_vague = 0
    for m in meta:
        if m.kind == "vague_answer":
            seen_vague += 1
            if seen_vague <= 2:
                raw += _META_WEIGHTS["vague_answer"]
        else:
            raw += _META_WEIGHTS.get(m.kind, 0.5)
    return min(raw, _META_UPLIFT_CAP)


def score_scenario(s: ScenarioInput) -> ScenarioScore:
    if not s.controls:
        coverage_index = 0.0
    else:
        total_w = sum(max(c.weight, 0.0) for c in s.controls)
        if total_w <= 0:
            coverage_index = 0.0
        else:
            coverage_index = (
                sum(effectiveness_score(c.coverage, c.effectiveness) * max(c.weight, 0.0)
                    for c in s.controls)
                / total_w
            )

    likelihood_reduction = round(coverage_index * 3)
    raw_uplift = _meta_uplift_value(s.meta_issues)
    meta_uplift_int = round(raw_uplift)

    inherent_l = max(1, min(4, s.inherent_likelihood))
    inherent_i = max(1, min(4, s.inherent_impact))

    residual_l = max(1, min(4, inherent_l - likelihood_reduction + meta_uplift_int))
    residual_i = inherent_i  # impact does not reduce; controls reduce likelihood
    band = band_for(residual_i, residual_l)

    return ScenarioScore(
        code=s.code,
        residual_impact=residual_i,
        residual_likelihood=residual_l,
        band=band,
        coverage_index=round(coverage_index, 3),
        likelihood_reduction=likelihood_reduction,
        meta_uplift=meta_uplift_int,
        rationale_breakdown={
            "inherent_impact": inherent_i,
            "inherent_likelihood": inherent_l,
            "coverage_index": round(coverage_index, 3),
            "likelihood_reduction_bands": likelihood_reduction,
            "meta_uplift_bands": meta_uplift_int,
            "meta_uplift_raw": round(raw_uplift, 3),
        },
    )


@dataclass
class AggregateScore:
    band: str
    rank: int
    weighted_mean_rank: float
    top2_mean_rank: float
    per_scenario: list[ScenarioScore]


def aggregate(scenarios: list[ScenarioScore], inherent_impacts: dict[str, int]) -> AggregateScore:
    if not scenarios:
        return AggregateScore(
            band="Low",
            rank=1,
            weighted_mean_rank=1.0,
            top2_mean_rank=1.0,
            per_scenario=[],
        )

    ranks = sorted((BAND_RANK[s.band] for s in scenarios), reverse=True)
    top2 = ranks[: min(2, len(ranks))]
    top2_mean = sum(top2) / len(top2)

    total_w = 0.0
    weighted_sum = 0.0
    for s in scenarios:
        w = float(inherent_impacts.get(s.code, s.residual_impact))
        total_w += w
        weighted_sum += BAND_RANK[s.band] * w
    weighted_mean = weighted_sum / total_w if total_w > 0 else float(ranks[0])

    rank = max(1, min(4, round(max(top2_mean, weighted_mean))))
    band = RANK_BAND[rank]

    return AggregateScore(
        band=band,
        rank=rank,
        weighted_mean_rank=round(weighted_mean, 2),
        top2_mean_rank=round(top2_mean, 2),
        per_scenario=scenarios,
    )
