from bench.metrics import score_exec_rubric, score_weakness_matching

EXPECTED = {
    "W1": {"severity": "high", "optional": False, "description": "no mfa"},
    "W2": {"severity": "medium", "optional": False, "description": "no drp test"},
    "W3": {"severity": "low", "optional": True, "description": "nice to find"},
}
ACTUAL_SEV = {101: "high", 102: "low", 103: "medium"}


def test_perfect_match():
    s = score_weakness_matching(
        matches=[
            {"expected_id": "W1", "actual_id": 101, "confidence": "high"},
            {"expected_id": "W2", "actual_id": 103, "confidence": "medium"},
        ],
        unmatched_expected=[{"expected_id": "W3"}],  # optional → not FN
        unmatched_actual=[],
        expected_by_id=EXPECTED,
        actual_severity_by_id=ACTUAL_SEV,
    )
    assert (s.tp, s.fp, s.fn) == (2, 0, 0)
    assert s.precision == s.recall == s.f1 == 1.0
    assert s.severity_exact == 1.0
    assert s.severity_mae == 0.0


def test_all_missed():
    s = score_weakness_matching(
        matches=[],
        unmatched_expected=[{"expected_id": "W1"}, {"expected_id": "W2"}],
        unmatched_actual=[{"actual_id": 101}],
        expected_by_id=EXPECTED,
        actual_severity_by_id=ACTUAL_SEV,
    )
    assert (s.tp, s.fp, s.fn) == (0, 1, 2)
    assert s.f1 == 0.0
    assert s.severity_exact is None  # no matched pairs


def test_low_confidence_degrades_to_fn_and_fp():
    s = score_weakness_matching(
        matches=[{"expected_id": "W1", "actual_id": 101, "confidence": "low"}],
        unmatched_expected=[{"expected_id": "W2"}],
        unmatched_actual=[],
        expected_by_id=EXPECTED,
        actual_severity_by_id=ACTUAL_SEV,
    )
    assert (s.tp, s.fp, s.fn) == (0, 1, 2)


def test_optional_matched_not_counted_as_tp():
    s = score_weakness_matching(
        matches=[
            {"expected_id": "W3", "actual_id": 102, "confidence": "high"},
            {"expected_id": "W1", "actual_id": 101, "confidence": "high"},
        ],
        unmatched_expected=[{"expected_id": "W2"}],
        unmatched_actual=[],
        expected_by_id=EXPECTED,
        actual_severity_by_id=ACTUAL_SEV,
    )
    # W3 is optional: matched but not a TP; W1 counts; W2 missed
    assert (s.tp, s.fp, s.fn) == (1, 0, 1)
    # both matches preserved for the UI
    assert len(s.counted_matches) == 2


def test_severity_disagreement():
    s = score_weakness_matching(
        matches=[{"expected_id": "W1", "actual_id": 102, "confidence": "high"}],
        unmatched_expected=[{"expected_id": "W2"}],
        unmatched_actual=[],
        expected_by_id=EXPECTED,
        actual_severity_by_id=ACTUAL_SEV,
    )
    assert s.severity_exact == 0.0
    assert s.severity_mae == 2.0  # high(3) vs low(1)


def test_empty_golden_and_empty_actual():
    s = score_weakness_matching(
        matches=[], unmatched_expected=[], unmatched_actual=[],
        expected_by_id={}, actual_severity_by_id={},
    )
    assert (s.tp, s.fp, s.fn) == (0, 0, 0)
    assert s.precision == s.recall == s.f1 == 0.0


def test_exec_rubric_scoring():
    s = score_exec_rubric(
        coverage_items=[
            {"point_id": "K1", "status": "covered"},
            {"point_id": "K2", "status": "partial"},
            {"point_id": "K3", "status": "missing"},
            {"point_id": "K4", "status": "unknown"},
        ],
        violation_items=[
            {"claim_id": "F1", "status": "violated"},
            {"claim_id": "F2", "status": "clean"},
        ],
        faithfulness_score=4,
        n_must_cover=4,
        n_must_not_claim=2,
    )
    assert s.coverage == 0.5      # (1 + 0.5 + 0 + 0.5) / 4
    assert s.violation == 0.5     # 1 - 1/2
    assert s.faithfulness == 0.8  # 4/5
    assert s.overall == 59.0      # 100*(0.5*0.5 + 0.3*0.8 + 0.2*0.5)


def test_exec_rubric_empty_lists():
    s = score_exec_rubric([], [], 5, 0, 0)
    assert s.coverage == 1.0 and s.violation == 1.0 and s.overall == 100.0


# ---- signal/noise classification + band error (Phase 0) ----

from bench.metrics import band_error, score_classification  # noqa: E402


def _cls(cat, golden=None, i=[0]):
    i[0] += 1
    return {"id": i[0], "category": cat, "golden": golden, "reason": "r"}


def test_classification_matches_manual_baseline_tabulation():
    # orbitclear baseline: TP 5, TP_OPT 1, DUP 4, LEGIT 8, BOILER 19, MISREAD 3 → 35 % signal
    rows = (
        [_cls("TP", f"G{k}") for k in range(5)]
        + [_cls("TP_OPTIONAL", "O1")]
        + [_cls("DUP_OF_TP", "G1"), _cls("DUP_OF_TP", "G1"), _cls("DUP_OF_TP", "G3"), _cls("DUP_OF_TP", "G5")]
        + [_cls("LEGIT_UNKEYED") for _ in range(8)]
        + [_cls("BOILERPLATE") for _ in range(19)]
        + [_cls("MISREAD") for _ in range(3)]
    )
    s = score_classification(rows)
    assert s.n == 40
    assert s.signal_share == 0.35
    assert s.dup_per_golden == round(4 / 6, 4)
    assert s.judge_fn == 0 and s.judge_fp_match == 0


def test_classification_judge_fn_is_signal_and_fp_match_is_noise():
    rows = [_cls("JUDGE_FN", "G1"), _cls("JUDGE_FP_MATCH", "G2"), _cls("DUP_OF_TP", "G1")]
    s = score_classification(rows)
    assert s.signal_share == round(1 / 3, 4)
    assert s.dup_per_golden == 1.0  # one golden hit (via JUDGE_FN), one dup
    assert s.judge_fn == 1 and s.judge_fp_match == 1


def test_classification_empty_and_no_golden_hit():
    assert score_classification([]).signal_share is None
    s = score_classification([_cls("BOILERPLATE"), _cls("DUP_OF_TP", "G1")])
    assert s.signal_share == 0.0
    assert s.dup_per_golden is None  # no primary hit to normalise by


def test_band_error_signed_and_none_when_unknown():
    assert band_error("VeryHigh", "High") == 1
    assert band_error("Moderate", "High") == -1
    assert band_error("High", "High") == 0
    assert band_error("VeryHigh", None) is None
    assert band_error("", "High") is None
