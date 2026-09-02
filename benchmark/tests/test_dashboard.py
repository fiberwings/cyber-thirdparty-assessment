"""Dashboard view-model + route tests (display-only; no scoring logic here)."""

import json

import pytest

from bench.costing import AssessmentLedger, Money, case_cost, case_time_s, judge_cost, judge_time_s
from bench.db import get_session
from bench.models import CaseResult, FindingMatch
from dashboard import viewmodel as vm


# ---------------- Money / costing ----------------


def test_money_add_propagates_flags_and_none():
    a = Money(1.0, estimated=True, missing=0)
    b = Money(2.0, estimated=False, missing=2)
    s = a + b
    assert (s.usd, s.estimated, s.missing) == (3.0, True, 2)
    assert (Money(None) + a).usd == 1.0          # None ≠ $0: unknown does not zero the sum
    assert Money(None).usd is None and not Money(None).known
    assert sum([a, b], Money(None)).usd == 3.0   # __radd__ with a Money start value


def test_case_cost_and_time():
    m = case_cost({"total_cost_usd": 2.5, "total_cost_estimated": True, "total_cost_missing": 3})
    assert (m.usd, m.estimated, m.missing) == (2.5, True, 3)
    assert case_cost(None).usd is None
    assert case_time_s({}) is None
    assert case_time_s({"create": 1, "report": 2.5}) == 3.5


class _JC:
    def __init__(self, ok, cost, source="estimated", latency=1000):
        self.ok, self.cost_usd, self.cost_source, self.latency_ms = ok, cost, source, latency


def test_judge_cost_and_time_semantics():
    calls = [_JC(True, 0.01), _JC(True, None, ""), _JC(False, None, latency=500)]
    m = judge_cost(calls)
    assert m.usd == pytest.approx(0.01) and m.estimated and m.missing == 1  # unpriced ok call
    assert judge_time_s(calls) == pytest.approx(2.5)                        # failed call's time counts


def test_ledger_counts_each_assessment_once():
    led = AssessmentLedger()
    assert led.record(7, Money(1.0)) is False
    assert led.record(7, Money(1.0)) is True
    assert led.record(None, Money(0.5)) is False
    assert led.distinct == 1
    assert led.total().usd == pytest.approx(1.5)


# ---------------- finding_status precedence ----------------


def _g(i="A-1", opt=False, retired=False):
    return vm.Golden(i, "desc", "high", opt, retired)


def _cr(status="ok"):
    return CaseResult(id=1, run_id=1, case_id="alpha", repetition=1, status=status,
                      error_stage="scenarios" if status == "error" else "", error_detail="x")


def _fm(kind, exp="A-1", actual=11, conf="high", counted=True, opt=False, asev="high"):
    return FindingMatch(match_type=kind, expected_id=exp, expected_description="desc",
                        expected_severity="high", expected_optional=opt, actual_weakness_id=actual,
                        actual_description="a", actual_severity=asev, confidence=conf, counted=counted)


def test_status_not_assessed_error_not_graded_not_in_key():
    assert vm.finding_status(_g(), None, [], None).status == "not_assessed"
    c = vm.finding_status(_g(), _cr("error"), [_fm("matched")], None)
    assert c.status == "error" and c.error_stage == "scenarios"
    assert vm.finding_status(_g(), _cr(), [], None).status == "not_graded"
    assert vm.finding_status(_g(), _cr(), [_fm("matched", exp="Z-9")], None).status == "not_in_key"


def test_status_matched_branch():
    assert vm.finding_status(_g(), _cr(), [_fm("matched")], None).status == "identified"
    cls = {"classification": [{"id": 11, "category": "JUDGE_FP_MATCH", "golden": "A-1"}]}
    c = vm.finding_status(_g(), _cr(), [_fm("matched")], cls)
    assert c.status == "false_match" and c.scored_as == "hit (TP)"
    c = vm.finding_status(_g(opt=True), _cr(), [_fm("matched", opt=True)], None)
    assert c.status == "identified_optional"
    c = vm.finding_status(_g(), _cr(), [_fm("matched", conf="low", counted=False)], None)
    assert c.status == "low_confidence" and "FN" in c.scored_as
    # prefer the counted row when several matched rows exist
    c = vm.finding_status(_g(), _cr(), [_fm("matched", conf="low", counted=False, actual=1),
                                        _fm("matched", actual=2)], None)
    assert c.status == "identified" and c.actual_weakness_id == 2


def test_status_flags_severity_and_dup():
    cls = {"classification": [{"id": 12, "category": "DUP_OF_TP", "golden": "A-1"}]}
    c = vm.finding_status(_g(), _cr(), [_fm("matched", asev="critical")], cls)
    assert c.sev_mismatch == "up" and c.dup
    assert vm.finding_status(_g(), _cr(), [_fm("matched", asev="low")], None).sev_mismatch == "down"
    assert vm.finding_status(_g(), _cr(), [_fm("matched")], None).sev_mismatch is None


def test_status_missed_branch():
    assert vm.finding_status(_g(), _cr(), [_fm("missed", actual=None)], None).status == "missed"
    cls = {"classification": [{"id": 14, "category": "JUDGE_FN", "golden": "A-1", "reason": "r"}]}
    c = vm.finding_status(_g(), _cr(), [_fm("missed", actual=None)], cls)
    assert c.status == "identified_by_judge" and c.scored_as == "miss (FN)" and c.category == "JUDGE_FN"
    assert vm.finding_status(_g(opt=True), _cr(), [_fm("missed", actual=None, opt=True)], None
                             ).status == "optional_missed"
    for flag, expect in ((True, "missed_reasoning"), (False, "missed_ingestion")):
        cls = {"missed_goldens": [{"golden": "A-1", "fact_in_chunks": flag, "where": "w", "note": "n"}]}
        c = vm.finding_status(_g(), _cr(), [_fm("missed", actual=None)], cls)
        assert c.status == expect and c.missed_where == "w"


def test_status_labels_cover_every_code():
    for code in vm.STATUS_ORDER:
        glyph, label, sentence = vm.STATUS_LABELS[code]
        assert label and sentence
        assert glyph or code == "not_assessed"
    assert vm.HIT_STATUSES.isdisjoint(vm.MISS_STATUSES)


# ---------------- golden index ----------------


def test_load_golden_index_is_lenient(tmp_cases):
    idx = vm.load_golden_index(tmp_cases)
    assert [g.id for g in idx["alpha"].goldens] == ["A-1", "A-2", "A-3"]
    assert idx["alpha"].required == 2 and idx["alpha"].vendor_name == "Alpha Ltd"
    assert idx["gamma"].load_error  # broken yaml surfaced, not raised


def test_with_legacy_goldens_appends_retired(tmp_cases):
    idx = vm.load_golden_index(tmp_cases)
    keyed = vm.with_legacy_goldens(idx, {"alpha": [_fm("missed", exp="A-9", actual=None)]})
    ids = [(g.id, g.retired) for g in keyed["alpha"].goldens]
    assert ids[-1] == ("A-9", True) and ids[0] == ("A-1", False)


# ---------------- summaries / totals / matrix ----------------


def test_build_index_summaries_and_totals(seeded_db):
    s = get_session()
    try:
        view = vm.build_index(s, include_empty=False)
    finally:
        s.close()
    by_id = {r.id: r for r in view.summaries}
    assert 3 not in by_id and view.hidden == 1
    r1 = by_id[1]
    assert (r1.goldens_identified, r1.goldens_required) == (2, 3)   # over ok cases only
    assert r1.n_ok == 2 and r1.n_error == 1 and r1.n_reps == 2
    alpha = next(v for v in r1.vendors if v.case_id == "alpha")
    assert [x.status for x in alpha.reps] == ["ok", "error"]
    assert r1.assess_cost.usd == pytest.approx(2.5) and r1.assess_cost.estimated
    assert r1.judge_cost.missing == 1 and r1.judge_time_s == pytest.approx(6.5)  # incl. the failed call
    r2 = by_id[2]
    assert r2.mode == "grade" and r2.n_reused == 1 and r2.assess_time_s is None
    assert r2.assessment_ids == [100]
    assert by_id[4].mode == "run" and by_id[4].judge_mode == "match"   # inferred
    assert by_id[5].judge_mode == "none"
    # totals span all runs (hidden included); assessment 100 is counted once across
    # runs 1+2, errored / unattributed case results count individually
    t = view.totals
    assert t.n_runs == 5 and t.n_assessments == 5 and t.n_unattributed == 1
    assert t.assess_cost.usd == pytest.approx(1.25 * 4)
    assert t.judge_cost.usd == pytest.approx(0.04)
    assert view.default_compare and all(i in by_id for i in view.default_compare)


def test_build_matrix_columns_rows_and_diff(seeded_db):
    s = get_session()
    try:
        m = vm.build_matrix(s, [1, 2])
        mf = vm.build_matrix(s, [1, 2], vendors=["beta"])
    finally:
        s.close()
    assert [c.key for c in m.subcolumns] == ["1:1", "1:2", "2:1"]
    alpha = next(g for g in m.groups if g.case_id == "alpha")
    assert [r.golden.id for r in alpha.rows] == ["A-1", "A-2", "A-3"]
    a1 = next(r for r in alpha.rows if r.golden.id == "A-1")
    assert [c.status for c in a1.cells] == ["identified", "error", "false_match"]
    assert a1.differs
    a2 = next(r for r in alpha.rows if r.golden.id == "A-2")
    assert [c.status for c in a2.cells] == ["missed_reasoning", "error", "identified_by_judge"]
    a3 = next(r for r in alpha.rows if r.golden.id == "A-3")
    # optional outranks low-confidence: an optional golden is never scored either way
    assert a3.cells[0].status == "optional_missed" and a3.cells[2].status == "identified_optional"
    beta = next(g for g in m.groups if g.case_id == "beta")
    assert [c.status for c in beta.rows[0].cells] == ["identified", "not_assessed", "not_assessed"]
    assert alpha.hit_counts[0] == (1, 2) and alpha.summaries[1].status == "error"
    assert alpha.summaries[2].costs.reused is True
    assert m.totals.n_assessments == 3  # 100 (once), 101, 102
    assert [g.case_id for g in mf.groups] == ["beta"]


def test_build_matrix_retired_golden(seeded_db):
    s = get_session()
    try:
        m = vm.build_matrix(s, [1, 4])
    finally:
        s.close()
    alpha = next(g for g in m.groups if g.case_id == "alpha")
    assert [r.golden.id for r in alpha.retired_rows] == ["A-9"]
    assert [c.status for c in alpha.retired_rows[0].cells] == ["not_in_key", "error", "missed"]


def test_run_and_case_detail(seeded_db):
    s = get_session()
    try:
        d = vm.build_run_detail(s, 1)
        cd = vm.build_case_detail(s, 1, seeded_db["alpha_r1"])
        assert vm.build_run_detail(s, 999) is None
        assert vm.build_case_detail(s, 2, seeded_db["alpha_r1"]) is None  # wrong run
    finally:
        s.close()
    assert d.prev_id is None and d.next_id == 2
    alpha = next(v for v in d.vendors if v.case_id == "alpha")
    rep1 = alpha.reps[0]
    assert [b.name for b in rep1.stages] == ["create", "documents", "gap_analysis", "report"]
    assert sum(b.share for b in rep1.stages) == pytest.approx(1.0)
    assert [p.purpose for p in rep1.purposes] == ["gap_analysis", "narrative"]
    assert rep1.purposes[0].cost.estimated and not rep1.purposes[1].cost.estimated
    assert d.charts["labels"] == ["alpha r1", "beta r1"]
    cats = [c for c, _, _ in cd.extras]
    assert cats == ["LEGIT_UNKEYED", "DUP_OF_TP"]
    assert cd.rep.summary.extras.counts == {"LEGIT_UNKEYED": 1, "DUP_OF_TP": 1}


# ---------------- routes ----------------


@pytest.fixture
def client(seeded_db):
    from fastapi.testclient import TestClient

    from dashboard.app import app

    return TestClient(app)


def test_index_hides_empty_runs_and_shows_flags(client):
    html = client.get("/").text
    assert 'id="run-1"' in html and 'id="run-3"' not in html
    assert "≈$" in html and "⚠" in html and "re-grades assessment #100" in html
    assert 'id="run-3"' in client.get("/?all=1").text


def test_run_and_case_pages(client, seeded_db):
    assert client.get("/runs/1").status_code == 200
    assert "retired" in client.get("/runs/4").text
    r = client.get(f"/runs/1/cases/{seeded_db['alpha_r1']}")
    assert r.status_code == 200 and "Missed · fact in evidence" in r.text
    assert client.get("/runs/999").status_code == 404
    assert client.get(f"/runs/2/cases/{seeded_db['alpha_r1']}").status_code == 404


def test_compare_routes(client):
    r = client.get("/compare?runs=1,2")
    assert r.status_code == 200
    assert "Identified · matcher missed" in r.text and "assessment #100" in r.text
    assert "retired from the key" in client.get("/compare?runs=1,4").text
    assert client.get("/compare?runs=1,999").status_code == 404
    assert client.get("/compare?runs=").status_code == 400
    assert client.get("/compare?runs=1,x").status_code == 400
    assert client.get("/compare?runs=1,2,3,4,5,6,7,8,9").status_code == 400
    r = client.get("/compare?a=1&b=2", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/compare?runs=1,2"
    assert client.get("/compare?runs=1&diff=1").status_code == 200


def test_api_keeps_legacy_keys_and_adds_new(client):
    runs = client.get("/api/runs").json()
    assert [r["id"] for r in runs] == [1, 2, 3, 4, 5]  # chronological, empties included
    r1 = runs[0]
    for k in ("id", "status", "mean_f1", "n_cases", "n_ok", "app_git_sha", "model_label",
              "mode", "judge_mode", "assess_cost_usd", "assess_cost_estimated",
              "assess_time_s", "judge_cost_usd", "judge_time_s",
              "goldens_identified", "goldens_required", "vendors"):
        assert k in r1, k
    one = client.get("/api/runs/1").json()
    assert {c["case_id"] for c in one["cases"]} == {"alpha", "beta"}
    assert any(g["status"] == "missed_reasoning" for c in one["cases"] for g in c["goldens"])
    cmp_ = client.get("/api/compare?runs=1,2").json()
    assert [c["key"] for c in cmp_["columns"]] == ["1:1", "1:2", "2:1"]
    assert client.get("/api/runs/999").status_code == 404
    json.dumps(cmp_)  # fully serialisable


def test_compat_reexports():
    from dashboard.app import _mean, _model_label, _run_summary, _sum_or_none  # noqa: F401

    assert _sum_or_none([1, None, 2]) == 3
