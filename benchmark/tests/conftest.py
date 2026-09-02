import pytest

import bench.db as db_mod
from bench.config import settings


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point the results DB at a temp file and reset the engine singleton."""
    monkeypatch.setattr(settings, "BENCH_DB_PATH", str(tmp_path / "bench.sqlite"))
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionLocal", None)
    yield
    monkeypatch.setattr(db_mod, "_engine", None)
    monkeypatch.setattr(db_mod, "_SessionLocal", None)


@pytest.fixture
def tmp_cases(tmp_path, monkeypatch):
    """Two minimal answer keys (alpha, beta) plus one broken yaml (gamma)."""
    root = tmp_path / "cases"
    (root / "alpha").mkdir(parents=True)
    (root / "alpha" / "case.yaml").write_text(
        "id: alpha\nvendor_name: Alpha Ltd\ngolden:\n  expected_band: Moderate\n"
        "  expected_weaknesses:\n"
        "    - {id: A-1, description: MFA not enforced, severity: high}\n"
        "    - {id: A-2, description: No pen test, severity: medium}\n"
        "    - {id: A-3, description: Nice to have, severity: low, optional: true}\n",
        encoding="utf-8",
    )
    (root / "beta").mkdir()
    (root / "beta" / "case.yaml").write_text(
        "id: beta\nvendor_name: Beta Inc\ngolden:\n  expected_band: High\n"
        "  expected_weaknesses:\n"
        "    - {id: B-1, description: Backups untested, severity: high}\n",
        encoding="utf-8",
    )
    (root / "gamma").mkdir()
    (root / "gamma" / "case.yaml").write_text("id: gamma\ngolden:\n  expected_band: Bogus\n",
                                              encoding="utf-8")
    monkeypatch.setattr(settings, "CASES_DIR", str(root))
    return root


@pytest.fixture
def seeded_db(fresh_db, tmp_cases):
    """Five runs covering the dashboard's edge cases; returns useful ids."""
    import json
    from datetime import datetime, timedelta

    from bench.models import CaseResult, FindingMatch, JudgeCall, Run

    t0 = datetime(2026, 8, 1, 10, 0, 0)
    tokens = json.dumps({
        "total_cost_usd": 1.25, "total_cost_estimated": True, "total_cost_missing": 0,
        "by_purpose": [
            {"purpose": "gap_analysis", "model_id": "x/reasoner", "calls": 2, "input_tokens": 1000,
             "output_tokens": 500, "latency_ms": 60000, "cost_usd": 1.0, "errors": 0,
             "cost_missing": 0, "cost_estimated": True},
            {"purpose": "narrative", "model_id": "x/fast", "calls": 1, "input_tokens": 100,
             "output_tokens": 50, "latency_ms": 5000, "cost_usd": 0.25, "errors": 0,
             "cost_missing": 0, "cost_estimated": False},
        ],
    })
    timings = json.dumps({"create": 1, "documents": 60, "gap_analysis": 120, "report": 5})
    cls_alpha = json.dumps({
        "chunk_scope": "full", "n_chunks_sent": 10, "counts": {"TP": 1},
        "classification": [
            {"id": 11, "category": "TP", "golden": "A-1", "reason": "matches"},
            {"id": 12, "category": "DUP_OF_TP", "golden": "A-1", "reason": "dup"},
            {"id": 13, "category": "LEGIT_UNKEYED", "golden": None, "reason": "real gap"},
        ],
        "missed_goldens": [{"golden": "A-2", "fact_in_chunks": True, "where": "doc 1 chunk 3",
                            "note": "stated plainly"}],
    })

    session = db_mod.get_session()
    ids = {}

    def add_run(rid, *, status="done", config=None, started=t0, notes=""):
        r = Run(id=rid, started_at=started, finished_at=started + timedelta(hours=1),
                status=status, backend_url="http://app", app_git_sha="abc1234def",
                judge_model="j/judge", judge_prompt_versions_json=json.dumps({"weakness_match": "wm-1"}),
                models_json=json.dumps({"fast": "x/fast", "reasoner": "x/reasoner"}),
                config_json=json.dumps(config if config is not None else {"mode": "run", "judge_mode": "full"}),
                notes=notes)
        session.add(r)
        session.flush()
        return r

    def add_case(run, case_id, rep=1, *, status="ok", assessment_id=None, tok=tokens, tim=timings,
                 cls=None, tp=None, fp=None, fn=None, band="", error_stage="", n=None):
        cr = CaseResult(run_id=run.id, case_id=case_id, repetition=rep, status=status,
                        assessment_id=assessment_id, timings_json=tim, tokens_json=tok,
                        classification_json=cls, tp=tp, fp=fp, fn=fn, aggregate_band=band,
                        error_stage=error_stage, error_detail="boom" if status == "error" else "",
                        n_weaknesses=n, started_at=run.started_at, finished_at=run.finished_at,
                        precision=(tp / (tp + fp)) if tp is not None and (tp + fp) else None,
                        recall=(tp / (tp + fn)) if tp is not None and (tp + fn) else None,
                        f1=None, exec_overall=80.0 if status == "ok" else None,
                        signal_share=0.5 if status == "ok" else None)
        session.add(cr)
        session.flush()
        return cr

    def add_match(cr, kind, exp_id=None, desc="", sev=None, opt=False, actual=None, adesc="",
                  asev=None, conf="high", counted=True, just=""):
        session.add(FindingMatch(case_result_id=cr.id, match_type=kind, expected_id=exp_id,
                                 expected_description=desc, expected_severity=sev,
                                 expected_optional=opt, actual_weakness_id=actual,
                                 actual_description=adesc, actual_severity=asev,
                                 confidence=conf, counted=counted, justification=just))

    def add_judge(cr, purpose, *, ok=True, cost=0.01, source="estimated", latency=2000):
        session.add(JudgeCall(case_result_id=cr.id, purpose=purpose, model_id="j/judge",
                              prompt_version="v1", latency_ms=latency, cost_usd=cost,
                              cost_source=source, ok=ok, error="" if ok else "timeout",
                              request_json="{}", response_json="{}"))

    # run 1: fresh run, judge full, 2 reps; alpha r1 ok / r2 error; beta ok
    r1 = add_run(1, notes="baseline")
    a1 = add_case(r1, "alpha", 1, assessment_id=100, cls=cls_alpha, tp=1, fp=2, fn=1, band="High", n=3)
    add_match(a1, "matched", "A-1", "MFA not enforced", "high", actual=11, adesc="No MFA",
              asev="critical", just="same gap")
    add_match(a1, "missed", "A-2", "No pen test", "medium", just="not reported")
    add_match(a1, "missed", "A-3", "Nice to have", "low", opt=True)
    add_match(a1, "extra", actual=12, adesc="MFA again", asev="high")
    add_match(a1, "extra", actual=13, adesc="Backups", asev="medium")
    add_judge(a1, "weakness_match")
    add_judge(a1, "finding_class", cost=None, source="")          # ok but unpriced
    add_judge(a1, "exec_rubric", ok=False, cost=None, latency=500)  # failed
    a2 = add_case(r1, "alpha", 2, status="error", assessment_id=101, error_stage="scenarios",
                  tok=None, tim="{}")
    b1 = add_case(r1, "beta", 1, assessment_id=102, tp=1, fp=0, fn=0, band="High", n=1)
    add_match(b1, "matched", "B-1", "Backups untested", "high", actual=21, adesc="Backups", asev="high")
    add_judge(b1, "weakness_match")
    ids.update(run1=1, alpha_r1=a1.id, alpha_r2=a2.id, beta_r1=b1.id)

    # run 2: grade mode re-using run-1 alpha assessment; JUDGE_FN + JUDGE_FP_MATCH + low confidence
    r2 = add_run(2, config={"mode": "grade", "judge_mode": "full"}, started=t0 + timedelta(days=1))
    cls2 = json.dumps({
        "chunk_scope": "full", "n_chunks_sent": 10, "counts": {},
        "classification": [
            {"id": 11, "category": "JUDGE_FP_MATCH", "golden": "A-1", "reason": "different gap"},
            {"id": 14, "category": "JUDGE_FN", "golden": "A-2", "reason": "actually reported"},
        ],
        "missed_goldens": [],
    })
    g = add_case(r2, "alpha", 1, assessment_id=100, tim="{}", cls=cls2, tp=1, fp=1, fn=1, band="Moderate", n=3)
    add_match(g, "matched", "A-1", "MFA not enforced", "high", actual=11, adesc="No MFA", asev="high")
    add_match(g, "missed", "A-2", "No pen test", "medium")
    add_match(g, "matched", "A-3", "Nice to have", "low", opt=True, actual=15, adesc="nice",
              asev="low", conf="low", counted=False)
    add_judge(g, "weakness_match")
    ids.update(run2=2, grade_alpha=g.id)

    # run 3: smoke run, zero ok cases
    r3 = add_run(3, status="failed", started=t0 + timedelta(days=2))
    add_case(r3, "alpha", 1, status="error", assessment_id=None, tok=None, tim="{}", error_stage="create")
    ids.update(run3=3)

    # run 4: legacy run without mode/judge_mode; graded against a retired golden A-9
    r4 = add_run(4, config={}, started=t0 + timedelta(days=3))
    l = add_case(r4, "alpha", 1, assessment_id=103, tp=1, fp=0, fn=1, band="Moderate", n=1)
    add_match(l, "matched", "A-1", "MFA not enforced", "high", actual=31, adesc="No MFA", asev="high")
    add_match(l, "missed", "A-9", "Old golden", "medium")
    add_judge(l, "weakness_match")
    ids.update(run4=4, legacy_alpha=l.id)

    # run 5: judge none — no finding_match rows at all
    r5 = add_run(5, config={"mode": "run", "judge_mode": "none"}, started=t0 + timedelta(days=4))
    add_case(r5, "beta", 1, assessment_id=104, n=2)
    ids.update(run5=5)

    session.commit()
    session.close()
    return ids
