"""Engine tests (Phase 5 semantics: state-based, confidence, bounded uplift)."""

from app.scoring.engine import (
    ControlInput,
    MetaIssueInput,
    ScenarioInput,
    WeaknessInput,
    aggregate,
    band_for,
    effectiveness_score,
    score_scenario,
)


def _ctrl(code="IAM.MFA", coverage="full", effectiveness="strong", weight=1.0):
    return ControlInput(code=code, name=code, weight=weight, coverage=coverage, effectiveness=effectiveness)


def test_effectiveness_table_edges():
    assert effectiveness_score("none", "strong") == 0.0
    assert effectiveness_score("full", "strong") == 1.0
    assert effectiveness_score("bogus", "bogus") == 0.3


def test_band_lookup_corners():
    assert band_for(1, 1) == "Low"
    assert band_for(4, 4) == "VeryHigh"
    assert band_for(4, 1) == "Moderate"  # rare but catastrophic stays visible


def test_strong_controls_drop_band():
    s = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=4, controls=[_ctrl()]))
    assert s.likelihood_reduction == 3 and s.residual_likelihood == 1
    assert s.band == "Moderate"  # impact floor holds


def test_meta_issues_report_confidence_but_never_move_the_score():
    base = ScenarioInput(code="X", inherent_impact=3, inherent_likelihood=2, controls=[_ctrl()])
    clean = score_scenario(base)
    noisy = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=2, controls=[_ctrl()],
        meta_issues=[MetaIssueInput(kind="insufficient_info"), MetaIssueInput(kind="missing_doc"),
                     MetaIssueInput(kind="vague_answer")]))
    assert clean.residual_likelihood == noisy.residual_likelihood
    assert clean.band == noisy.band
    assert clean.confidence == "high" and noisy.confidence == "low"
    one_vague = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=2, controls=[_ctrl()],
        meta_issues=[MetaIssueInput(kind="vague_answer")]))
    assert one_vague.confidence == "medium"


def test_no_controls_means_no_reduction():
    s = score_scenario(ScenarioInput(code="X", inherent_impact=2, inherent_likelihood=3))
    assert s.coverage_index == 0.0 and s.residual_likelihood == 3


def _score(band, code="A", confidence="high", impact=3):
    from app.scoring.engine import ScenarioScore
    from app.scoring.tables import BAND_RANK
    return ScenarioScore(
        code=code, residual_impact=impact, residual_likelihood=BAND_RANK[band], band=band,
        coverage_index=0.5, likelihood_reduction=1, uplift=0, distinct_high_critical=0,
        auditor_tested_high_critical=0, confidence=confidence, state_downgrades=[],
        rationale_breakdown={})


def test_aggregate_weighted_mean_drives_band_not_top2():
    # One VeryHigh outlier among Moderates: old top-2 rule forced High/VeryHigh;
    # the weighted view keeps the overall at the population's level.
    scores = [_score("VeryHigh", "A"), _score("Moderate", "B"), _score("Moderate", "C"),
              _score("Moderate", "D"), _score("Moderate", "E")]
    agg = aggregate(scores, {c: 3 for c in "ABCDE"})
    assert agg.band == "Moderate" and agg.top2_mean_rank == 3.0


def test_aggregate_veryhigh_needs_two_scenarios_or_weighted_mean():
    two_vh = [_score("VeryHigh", "A"), _score("VeryHigh", "B"), _score("Low", "C")]
    assert aggregate(two_vh, {c: 3 for c in "ABC"}).band == "VeryHigh"
    all_vh = [_score("VeryHigh", "A"), _score("VeryHigh", "B")]
    assert aggregate(all_vh, {c: 3 for c in "AB"}).band == "VeryHigh"
    one_vh = [_score("VeryHigh", "A"), _score("Low", "B"), _score("Low", "C")]
    assert aggregate(one_vh, {c: 3 for c in "ABC"}).band != "VeryHigh"


def test_aggregate_confidence_is_worst_scenario():
    scores = [_score("Moderate", "A", confidence="high"), _score("Moderate", "B", confidence="medium")]
    assert aggregate(scores, {"A": 3, "B": 3}).confidence == "medium"


def test_aggregate_boundary_rounds_half_up_not_bankers():
    # weighted mean exactly 2.5 must resolve upward (High), not down via
    # Python's round-half-to-even.
    scores = [_score("High", "A"), _score("Moderate", "B")]
    agg = aggregate(scores, {"A": 3, "B": 3})
    assert agg.weighted_mean_rank == 2.5 and agg.band == "High"
