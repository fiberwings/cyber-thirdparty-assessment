"""Pure metric computations. No I/O, no LLM — unit-testable.

The judge produces raw match/rubric structures; every number reported by the
benchmark is derived here, so results stay comparable across judge prompt
versions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}

# Exec-summary aggregation weights (recorded in run.config_json for provenance)
EXEC_WEIGHTS = {"coverage": 0.5, "faithfulness": 0.3, "violation": 0.2}


@dataclass
class WeaknessScores:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    severity_exact: float | None = None
    severity_mae: float | None = None
    # matched pairs that counted, for persistence: (expected_id, actual_id, confidence)
    counted_matches: list[tuple[str, int, str]] = field(default_factory=list)


def score_weakness_matching(
    matches: list[dict],
    unmatched_expected: list[dict],
    unmatched_actual: list[dict],
    expected_by_id: dict[str, dict],
    actual_severity_by_id: dict[int, str],
    confidence_threshold: tuple[str, ...] = ("high", "medium"),
) -> WeaknessScores:
    """Turn judge output into precision/recall/F1 + severity agreement.

    - A judge match only counts as TP when its confidence meets the threshold;
      below-threshold matches degrade to FN (golden side) + FP (actual side),
      unless the golden item is optional.
    - Golden items marked optional never contribute to TP/FN, and an actual
      matched to an optional golden item is not a FP either.
    """
    s = WeaknessScores()
    sev_diffs: list[int] = []

    optional_ids = {eid for eid, e in expected_by_id.items() if e.get("optional")}

    for m in matches:
        eid, aid = m["expected_id"], m["actual_id"]
        counted = m.get("confidence") in confidence_threshold
        is_optional = eid in optional_ids
        if counted:
            if not is_optional:
                s.tp += 1
                exp_sev = expected_by_id[eid]["severity"]
                act_sev = actual_severity_by_id.get(aid)
                if act_sev in SEVERITY_RANK:
                    sev_diffs.append(
                        abs(SEVERITY_RANK[exp_sev] - SEVERITY_RANK[act_sev])
                    )
            s.counted_matches.append((eid, aid, m.get("confidence", "")))
        else:
            if not is_optional:
                s.fn += 1
                s.fp += 1

    for u in unmatched_expected:
        if u["expected_id"] not in optional_ids:
            s.fn += 1
    s.fp += len(unmatched_actual)

    s.precision = s.tp / (s.tp + s.fp) if (s.tp + s.fp) else 0.0
    s.recall = s.tp / (s.tp + s.fn) if (s.tp + s.fn) else 0.0
    s.f1 = (
        2 * s.precision * s.recall / (s.precision + s.recall)
        if (s.precision + s.recall)
        else 0.0
    )
    if sev_diffs:
        s.severity_exact = sum(1 for d in sev_diffs if d == 0) / len(sev_diffs)
        s.severity_mae = sum(sev_diffs) / len(sev_diffs)
    return s


@dataclass
class ExecScores:
    coverage: float
    faithfulness: float
    violation: float
    overall: float


def score_exec_rubric(
    coverage_items: list[dict],
    violation_items: list[dict],
    faithfulness_score: int,
    n_must_cover: int,
    n_must_not_claim: int,
) -> ExecScores:
    """covered=1, partial/unknown=0.5, missing=0; violation penalises only
    definite 'violated'; unknown treated as non-violation (flagged in the UI,
    not the score)."""
    if n_must_cover:
        pts = 0.0
        for c in coverage_items:
            if c["status"] == "covered":
                pts += 1.0
            elif c["status"] in ("partial", "unknown"):
                pts += 0.5
        coverage = pts / n_must_cover
    else:
        coverage = 1.0

    if n_must_not_claim:
        violated = sum(1 for v in violation_items if v["status"] == "violated")
        violation = 1.0 - violated / n_must_not_claim
    else:
        violation = 1.0

    faithfulness = max(0, min(5, faithfulness_score)) / 5.0
    overall = 100.0 * (
        EXEC_WEIGHTS["coverage"] * coverage
        + EXEC_WEIGHTS["faithfulness"] * faithfulness
        + EXEC_WEIGHTS["violation"] * violation
    )
    return ExecScores(
        coverage=round(coverage, 4),
        faithfulness=round(faithfulness, 4),
        violation=round(violation, 4),
        overall=round(overall, 2),
    )
