"""Phase 5: weaknesses change control state; uplift is bounded and evidence-gated."""

from app.scoring.engine import ControlInput, ScenarioInput, WeaknessInput, score_scenario


def _ctrl(code="IAM.MFA", coverage="full", effectiveness="strong"):
    return ControlInput(code=code, name=code, weight=1.0, coverage=coverage, effectiveness=effectiveness)


def _w(sev, codes=("IAM.MFA",), strength="vendor_admitted"):
    return WeaknessInput(severity=sev, mapped_control_codes=list(codes), evidence_strength=strength)


def test_high_severity_forces_control_to_weak_state():
    s = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=3,
        controls=[_ctrl()], weaknesses=[_w("high")]))
    # full/strong 1.0 → full/weak 0.5 → reduction 2 (was 3)
    assert s.coverage_index == 0.5 and s.likelihood_reduction == 2
    assert s.state_downgrades == ["IAM.MFA: strong→weak (high)"]


def test_medium_caps_at_adequate_low_changes_nothing():
    s = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=3,
        controls=[_ctrl()], weaknesses=[_w("medium")]))
    assert s.coverage_index == 0.8 and s.state_downgrades == ["IAM.MFA: strong→adequate (medium)"]
    s2 = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=3,
        controls=[_ctrl()], weaknesses=[_w("low")]))
    assert s2.coverage_index == 1.0 and s2.state_downgrades == []


def test_many_medium_rows_add_no_uplift():
    """The additive-by-count failure: 40 mediums used to max the residual."""
    s = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=3,
        controls=[_ctrl()], weaknesses=[_w("medium") for _ in range(40)]))
    assert s.uplift == 0
    assert s.residual_likelihood <= 3  # never above inherent


def test_uplift_needs_one_critical_or_two_high():
    one_high = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=2,
        controls=[_ctrl(coverage="none", effectiveness="unknown")], weaknesses=[_w("high")]))
    assert one_high.uplift == 0
    two_high = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=2,
        controls=[_ctrl(coverage="none", effectiveness="unknown")],
        weaknesses=[_w("high"), _w("high", codes=("ENC.REST",))]))
    assert two_high.uplift == 1
    one_crit = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=2,
        controls=[_ctrl(coverage="none", effectiveness="unknown")], weaknesses=[_w("critical")]))
    assert one_crit.uplift == 1


def test_residual_capped_at_inherent_unless_auditor_tested():
    vendor_only = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=2,
        controls=[_ctrl(coverage="none", effectiveness="unknown")],
        weaknesses=[_w("critical"), _w("high", codes=("ENC.REST",))]))
    assert vendor_only.residual_likelihood == 2  # uplift earned but capped at inherent
    audited = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=2,
        controls=[_ctrl(coverage="none", effectiveness="unknown")],
        weaknesses=[_w("critical", strength="auditor_tested")]))
    assert audited.residual_likelihood == 3  # may exceed inherent by exactly one
    assert audited.rationale_breakdown["residual_ceiling"] == 3


def test_unmapped_weakness_does_not_affect_score():
    s = score_scenario(ScenarioInput(
        code="X", inherent_impact=3, inherent_likelihood=3,
        controls=[_ctrl()], weaknesses=[WeaknessInput(severity="critical", mapped_control_codes=[])]))
    assert s.coverage_index == 1.0 and s.uplift == 0 and s.distinct_high_critical == 0
