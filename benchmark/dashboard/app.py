"""Benchmark results dashboard — read-only over bench.sqlite.

    .venv/bin/uvicorn dashboard.app:app --port 8100
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from bench.db import get_session
from bench.models import CaseResult, FindingMatch, JudgeCall, Run

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="TPRM Benchmark Dashboard")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _mean(values: list) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def _model_label(run: Run) -> str:
    """Short human label for the model config: overrides if set, else defaults."""
    try:
        models = json.loads(run.models_json or "{}")
    except json.JSONDecodeError:
        return "?"
    overrides = models.get("overrides") or {}
    if overrides:
        uniq = sorted(set(overrides.values()))
        label = ", ".join(m.split("/")[-1] for m in uniq)
        return f"pinned: {label}"
    profiles = models.get("profiles") or []
    parts = [f"{p['name']}={p['default_model'].split('/')[-1]}" for p in profiles]
    return ", ".join(parts) or "defaults"


def _run_summary(run: Run, results: list[CaseResult]) -> dict:
    ok = [r for r in results if r.status == "ok"]
    return {
        "id": run.id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "status": run.status,
        "app_git_sha": (run.app_git_sha or "")[:7],
        "app_git_dirty": run.app_git_dirty,
        "app_version": run.app_version,
        "model_label": _model_label(run),
        "judge_model": run.judge_model,
        "notes": run.notes,
        "n_cases": len(results),
        "n_ok": len(ok),
        "mean_f1": _mean([r.f1 for r in ok]),
        "mean_exec": _mean([r.exec_overall for r in ok]),
    }


# ---------------- HTML views ----------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    session = get_session()
    try:
        runs = session.scalars(select(Run).order_by(Run.started_at)).all()
        results_by_run: dict[int, list[CaseResult]] = defaultdict(list)
        for cr in session.scalars(select(CaseResult)).all():
            results_by_run[cr.run_id].append(cr)

        summaries = [_run_summary(r, results_by_run.get(r.id, [])) for r in runs]

        # Trend series: per case, mean metric across reps for each run
        case_ids = sorted({cr.case_id for crs in results_by_run.values() for cr in crs})
        trend = {"labels": [], "runs": [], "f1": {}, "exec": {}}
        for s in summaries:
            trend["labels"].append(
                f"#{s['id']} · {(s['started_at'] or '')[:10]}"
            )
            trend["runs"].append(
                {"id": s["id"], "sha": s["app_git_sha"], "models": s["model_label"]}
            )
        for cid in case_ids:
            f1_series, exec_series = [], []
            for r in runs:
                ok = [
                    cr for cr in results_by_run.get(r.id, [])
                    if cr.case_id == cid and cr.status == "ok"
                ]
                f1_series.append(_mean([cr.f1 for cr in ok]))
                exec_series.append(_mean([cr.exec_overall for cr in ok]))
            trend["f1"][cid] = f1_series
            trend["exec"][cid] = exec_series

        return templates.TemplateResponse(
            request,
            "index.html",
            {"summaries": list(reversed(summaries)), "trend_json": json.dumps(trend)},
        )
    finally:
        session.close()


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_view(request: Request, run_id: int):
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if not run:
            raise HTTPException(404, "run not found")
        results = session.scalars(
            select(CaseResult)
            .where(CaseResult.run_id == run_id)
            .order_by(CaseResult.case_id, CaseResult.repetition)
        ).all()

        rows = []
        for cr in results:
            tokens = json.loads(cr.tokens_json) if cr.tokens_json else None
            timings = json.loads(cr.timings_json or "{}")
            rows.append(
                {
                    "id": cr.id,
                    "case_id": cr.case_id,
                    "repetition": cr.repetition,
                    "status": cr.status,
                    "error_stage": cr.error_stage,
                    "error_detail": cr.error_detail,
                    "band": cr.aggregate_band,
                    "precision": cr.precision,
                    "recall": cr.recall,
                    "f1": cr.f1,
                    "severity_exact": cr.severity_exact,
                    "severity_mae": cr.severity_mae,
                    "signal_share": cr.signal_share,
                    "dup_per_golden": cr.dup_per_golden,
                    "judge_fn": cr.judge_fn,
                    "band_error": cr.band_error,
                    "n_weaknesses": cr.n_weaknesses,
                    "exec_coverage": cr.exec_coverage,
                    "exec_faithfulness": cr.exec_faithfulness,
                    "exec_violation": cr.exec_violation,
                    "exec_overall": cr.exec_overall,
                    "duration_s": round(sum(timings.values()), 1) if timings else None,
                    "cost_usd": (tokens or {}).get("total_cost_usd"),
                    "tokens": (tokens or {}).get("total_output_tokens"),
                }
            )

        ok_rows = [r for r in rows if r["status"] == "ok"]
        labels = [f"{r['case_id']} r{r['repetition']}" for r in ok_rows]
        charts = {
            "labels": labels,
            "precision": [r["precision"] for r in ok_rows],
            "recall": [r["recall"] for r in ok_rows],
            "f1": [r["f1"] for r in ok_rows],
            "exec_coverage": [r["exec_coverage"] for r in ok_rows],
            "exec_faithfulness": [r["exec_faithfulness"] for r in ok_rows],
            "exec_violation": [r["exec_violation"] for r in ok_rows],
        }

        meta = _run_summary(run, results)
        meta["config"] = json.loads(run.config_json or "{}")
        meta["models"] = json.loads(run.models_json or "{}")
        meta["judge_prompts"] = json.loads(run.judge_prompt_versions_json or "{}")
        meta["full_sha"] = run.app_git_sha
        meta["backend_url"] = run.backend_url
        meta["finished_at"] = run.finished_at.isoformat() if run.finished_at else None

        return templates.TemplateResponse(
            request,
            "run.html",
            {"meta": meta, "rows": rows, "charts_json": json.dumps(charts)},
        )
    finally:
        session.close()


@app.get("/runs/{run_id}/cases/{case_result_id}", response_class=HTMLResponse)
def case_view(request: Request, run_id: int, case_result_id: int):
    session = get_session()
    try:
        cr = session.get(CaseResult, case_result_id)
        if not cr or cr.run_id != run_id:
            raise HTTPException(404, "case result not found")

        matches = session.scalars(
            select(FindingMatch).where(FindingMatch.case_result_id == cr.id)
        ).all()
        judge_calls = session.scalars(
            select(JudgeCall)
            .where(JudgeCall.case_result_id == cr.id)
            .order_by(JudgeCall.id)
        ).all()

        report = json.loads(cr.report_json) if cr.report_json else {}
        exec_summary = report.get("executive_summary")
        classification = json.loads(cr.classification_json) if cr.classification_json else None
        category_by_id = {
            c["id"]: c for c in (classification or {}).get("classification", [])
        }
        timings = json.loads(cr.timings_json or "{}")
        tokens = json.loads(cr.tokens_json) if cr.tokens_json else None

        # Rubric breakdown from the exec_rubric judge call (last successful one)
        rubric = None
        for jc in judge_calls:
            if jc.purpose == "exec_rubric" and jc.ok:
                try:
                    rubric = json.loads(jc.response_json)
                except json.JSONDecodeError:
                    rubric = None

        return templates.TemplateResponse(
            request,
            "case.html",
            {
                "cr": cr,
                "run_id": run_id,
                "matched": [m for m in matches if m.match_type == "matched"],
                "missed": [m for m in matches if m.match_type == "missed"],
                "extra": [m for m in matches if m.match_type == "extra"],
                "judge_calls": judge_calls,
                "rubric": rubric,
                "classification": classification,
                "category_by_id": category_by_id,
                "exec_summary": exec_summary,
                "timings": timings,
                "tokens": tokens,
            },
        )
    finally:
        session.close()


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, a: int, b: int):
    session = get_session()
    try:
        run_a, run_b = session.get(Run, a), session.get(Run, b)
        if not run_a or not run_b:
            raise HTTPException(404, "run not found")

        def case_agg(run_id: int) -> dict[str, dict]:
            results = session.scalars(
                select(CaseResult).where(
                    CaseResult.run_id == run_id, CaseResult.status == "ok"
                )
            ).all()
            by_case: dict[str, list[CaseResult]] = defaultdict(list)
            for cr in results:
                by_case[cr.case_id].append(cr)
            agg = {}
            for cid, crs in by_case.items():
                agg[cid] = {
                    "f1": _mean([c.f1 for c in crs]),
                    "recall": _mean([c.recall for c in crs]),
                    "precision": _mean([c.precision for c in crs]),
                    "exec_overall": _mean([c.exec_overall for c in crs]),
                    "ids": [c.id for c in crs],
                }
            return agg

        agg_a, agg_b = case_agg(a), case_agg(b)
        case_ids = sorted(set(agg_a) | set(agg_b))
        delta_rows = []
        for cid in case_ids:
            ra, rb = agg_a.get(cid), agg_b.get(cid)
            row = {"case_id": cid, "a": ra, "b": rb, "deltas": {}}
            if ra and rb:
                for k in ("f1", "recall", "precision", "exec_overall"):
                    if ra[k] is not None and rb[k] is not None:
                        row["deltas"][k] = round(rb[k] - ra[k], 4)
            delta_rows.append(row)

        # Newly missed / newly hallucinated by golden-id set difference
        def match_sets(agg: dict[str, dict]) -> dict[str, dict[str, set]]:
            out = {}
            for cid, info in agg.items():
                fm = session.scalars(
                    select(FindingMatch).where(
                        FindingMatch.case_result_id.in_(info["ids"])
                    )
                ).all()
                out[cid] = {
                    "missed": {m.expected_id for m in fm if m.match_type == "missed"},
                    "extra_desc": {
                        m.actual_description for m in fm if m.match_type == "extra"
                    },
                }
            return out

        sets_a, sets_b = match_sets(agg_a), match_sets(agg_b)
        diffs = {}
        for cid in case_ids:
            sa = sets_a.get(cid, {"missed": set(), "extra_desc": set()})
            sb = sets_b.get(cid, {"missed": set(), "extra_desc": set()})
            diffs[cid] = {
                "newly_missed": sorted(sb["missed"] - sa["missed"]),
                "fixed_missed": sorted(sa["missed"] - sb["missed"]),
                "newly_extra": sorted(sb["extra_desc"] - sa["extra_desc"]),
            }

        results_a = session.scalars(select(CaseResult).where(CaseResult.run_id == a)).all()
        results_b = session.scalars(select(CaseResult).where(CaseResult.run_id == b)).all()
        return templates.TemplateResponse(
            request,
            "compare.html",
            {
                "meta_a": _run_summary(run_a, results_a),
                "meta_b": _run_summary(run_b, results_b),
                "delta_rows": delta_rows,
                "diffs": diffs,
            },
        )
    finally:
        session.close()


# ---------------- JSON API ----------------


@app.get("/api/runs")
def api_runs():
    session = get_session()
    try:
        runs = session.scalars(select(Run).order_by(Run.started_at)).all()
        results_by_run: dict[int, list[CaseResult]] = defaultdict(list)
        for cr in session.scalars(select(CaseResult)).all():
            results_by_run[cr.run_id].append(cr)
        return [_run_summary(r, results_by_run.get(r.id, [])) for r in runs]
    finally:
        session.close()


@app.get("/api/runs/{run_id}")
def api_run(run_id: int):
    session = get_session()
    try:
        run = session.get(Run, run_id)
        if not run:
            raise HTTPException(404, "run not found")
        results = session.scalars(
            select(CaseResult).where(CaseResult.run_id == run_id)
        ).all()
        summary = _run_summary(run, results)
        summary["cases"] = [
            {
                "id": cr.id,
                "case_id": cr.case_id,
                "repetition": cr.repetition,
                "status": cr.status,
                "precision": cr.precision,
                "recall": cr.recall,
                "f1": cr.f1,
                "exec_overall": cr.exec_overall,
                "aggregate_band": cr.aggregate_band,
            }
            for cr in results
        ]
        return summary
    finally:
        session.close()
