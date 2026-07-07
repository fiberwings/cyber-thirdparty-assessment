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
