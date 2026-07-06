from app.scoring.engine import (
    ControlInput,
    MetaIssueInput,
    ScenarioInput,
    aggregate,
    band_for,
    effectiveness_score,
    score_scenario,
)


def test_effectiveness_table_edges():
    assert effectiveness_score("none", "strong") == 0.0
    assert effectiveness_score("full", "strong") == 1.0
    assert effectiveness_score("partial", "weak") == 0.25
    assert effectiveness_score("full", "unknown") == 0.45
    # Defensive default
    assert effectiveness_score("garbage", "garbage") == 0.3


def test_band_lookup_corners():
    assert band_for(1, 1) == "Low"
    assert band_for(4, 4) == "VeryHigh"
    assert band_for(4, 1) == "Moderate"
    assert band_for(1, 4) == "Moderate"
    # out-of-range clamped
    assert band_for(99, -1) == "Moderate"


def test_strong_controls_drop_band_but_impact_caps():
    s = ScenarioInput(
        code="X",
        inherent_impact=4,
        inherent_likelihood=4,
        controls=[ControlInput("c1", "x", 1.0, "full", "strong")],
    )
    r = score_scenario(s)
    # 1.0 * 3 = 3 reduction → likelihood 1, impact stays 4 → High
    assert r.residual_likelihood == 1
    assert r.residual_impact == 4
    assert r.band == "Moderate"


def test_meta_uplift_capped():
    s = ScenarioInput(
        code="X",
        inherent_impact=2,
        inherent_likelihood=2,
        controls=[ControlInput("c1", "x", 1.0, "full", "strong")],
        meta_issues=[
            MetaIssueInput("conflicting_evidence"),
            MetaIssueInput("conflicting_evidence"),
            MetaIssueInput("conflicting_evidence"),
        ],
    )
    r = score_scenario(s)
    # Uplift cap = 2; reduction = 3 → -3 + 2 = -1 → likelihood = 1
    assert r.combined_uplift == 2
    assert r.residual_likelihood == 1


def test_vague_answer_capped_to_two():
    s = ScenarioInput(
        code="X",
        inherent_impact=3,
        inherent_likelihood=2,
        controls=[ControlInput("c1", "x", 1.0, "full", "adequate")],
        meta_issues=[MetaIssueInput("vague_answer")] * 5,
    )
    r = score_scenario(s)
    # Only first 2 vague answers count → 0.5 + 0.5 = 1.0 → rounded uplift = 1
    assert r.combined_uplift == 1
    assert r.meta_uplift_raw == 1.0


def test_aggregate_takes_higher_of_top2_and_weighted():
    s1 = ScenarioInput(
        code="A", inherent_impact=4, inherent_likelihood=4,
        controls=[ControlInput("c", "x", 1.0, "none", "weak")],
    )
    s2 = ScenarioInput(
        code="B", inherent_impact=2, inherent_likelihood=2,
        controls=[ControlInput("c", "x", 1.0, "full", "strong")],
    )
    r1, r2 = score_scenario(s1), score_scenario(s2)
    agg = aggregate([r1, r2], {"A": 4, "B": 2})
    assert agg.band in {"VeryHigh", "High"}  # dominated by s1
    assert agg.rank >= 3


def test_no_controls_means_no_reduction():
    s = ScenarioInput(code="X", inherent_impact=2, inherent_likelihood=3, controls=[])
    r = score_scenario(s)
    assert r.coverage_index == 0.0
    assert r.residual_likelihood == 3
