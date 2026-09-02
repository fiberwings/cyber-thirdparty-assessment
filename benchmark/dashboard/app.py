"""Benchmark results dashboard — read-only over bench.sqlite.

    .venv/bin/uvicorn dashboard.app:app --port 8100

Routes are thin: open a session, build a view-model (dashboard/viewmodel.py),
render a template. All derivation lives in the view-model so it is testable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import select

from bench.config import settings
from bench.costing import Money
from bench.db import get_session
from bench.models import Run

from . import viewmodel as vm
from .viewmodel import _mean, _model_label, _run_summary, _sum_or_none  # noqa: F401  (compat re-exports)

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="TPRM Benchmark Dashboard")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------------- Jinja filters ----------------


def fmt_money(m: Money | None, digits: int = 2, reused_note: bool = True) -> Markup:
    """`—` unknown · `≈$x` estimated · `$x ⚠` under-reported · `(reused)` re-reported."""
    if m is None or m.usd is None:
        return Markup('<span class="money money--unknown" title="nothing priced">—</span>')
    classes = ["money"]
    prefix = ""
    title = []
    if m.estimated:
        prefix = "≈"
        classes.append("money--est")
        title.append("contains list-price × tokens estimates, not metered cost")
    if m.missing:
        classes.append("money--missing")
        title.append(f"{m.missing} live call(s) recorded no cost — under-reported, not free")
    if m.reused:
        classes.append("money--reused")
        title.append("re-reports spend of an assessment already counted for an earlier run")
    text = f"{prefix}${m.usd:,.{digits}f}"
    if m.missing:
        text += " ⚠"
    if m.reused and reused_note:
        text += ' <span class="money-reused-tag">reused</span>'
    return Markup(f'<span class="{" ".join(classes)}" title="{escape("; ".join(title))}">{text}</span>')


def fmt_secs(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m}m {sec:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def fmt_pct(v: float | None, digits: int = 0) -> str:
    return "—" if v is None else f"{100 * v:.{digits}f}%"


def fmt_num(v: float | None, digits: int = 2) -> str:
    return "—" if v is None else f"{v:.{digits}f}"


def fmt_detail(obj) -> str:
    """Dataclass → JSON string for `data-*` attributes (autoescaped by Jinja)."""
    return json.dumps(asdict(obj) if is_dataclass(obj) else obj, ensure_ascii=False)


def fmt_dt(iso: str | None, with_time: bool = True) -> str:
    if not iso:
        return "—"
    return iso[:16].replace("T", " ") if with_time else iso[:10]


templates.env.filters.update(
    money=fmt_money, secs=fmt_secs, pct=fmt_pct, num=fmt_num, dt=fmt_dt, detail=fmt_detail,
)
templates.env.globals.update(
    STATUS_LABELS=vm.STATUS_LABELS, STATUS_ORDER=vm.STATUS_ORDER,
    EXTRA_CATEGORIES=vm.EXTRA_CATEGORIES, EXTRA_LABELS=vm.EXTRA_LABELS,
    DB_PATH=settings.BENCH_DB_PATH,
)


def _render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _parse_run_ids(raw: str | None) -> list[int]:
    if raw is None or not raw.strip():
        raise HTTPException(400, "runs= is required, e.g. /compare?runs=18,20,21")
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise HTTPException(400, f"bad run id {part!r}")
        rid = int(part)
        if rid not in ids:
            ids.append(rid)
    if not ids:
        raise HTTPException(400, "runs= is required, e.g. /compare?runs=18,20,21")
    if len(ids) > vm.MAX_COMPARE_RUNS:
        raise HTTPException(400, f"at most {vm.MAX_COMPARE_RUNS} runs can be compared")
    return ids


def _check_runs_exist(session, ids: list[int]) -> None:
    found = set(session.scalars(select(Run.id).where(Run.id.in_(ids))).all())
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(404, f"run(s) not found: {missing}")


def _vendor_list(raw: str | None) -> list[str] | None:
    return [v.strip() for v in (raw or "").split(",") if v.strip()] or None


# ---------------- HTML views ----------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request, all: int = 0):
    session = get_session()
    try:
        view = vm.build_index(session, include_empty=bool(all))
        return _render(request, "index.html", {"view": view, "trend_json": json.dumps(view.trend)})
    finally:
        session.close()


@app.get("/compare", response_class=HTMLResponse)
def compare(request: Request, runs: str | None = None, vendors: str | None = None,
            diff: int = 0, a: int | None = None, b: int | None = None):
    if runs is None and a is not None and b is not None:  # legacy pairwise URL
        return RedirectResponse(f"/compare?runs={a},{b}", status_code=307)
    ids = _parse_run_ids(runs)
    session = get_session()
    try:
        _check_runs_exist(session, ids)
        matrix = vm.build_matrix(session, ids, _vendor_list(vendors))
        return _render(request, "compare.html", {"m": matrix, "diff_only": bool(diff)})
    finally:
        session.close()


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_view(request: Request, run_id: int):
    session = get_session()
    try:
        detail = vm.build_run_detail(session, run_id)
        if detail is None:
            raise HTTPException(404, "run not found")
        return _render(request, "run.html", {"d": detail, "charts_json": json.dumps(detail.charts)})
    finally:
        session.close()


@app.get("/runs/{run_id}/cases/{case_result_id}", response_class=HTMLResponse)
def case_view(request: Request, run_id: int, case_result_id: int):
    session = get_session()
    try:
        detail = vm.build_case_detail(session, run_id, case_result_id)
        if detail is None:
            raise HTTPException(404, "case result not found")
        return _render(request, "case.html", {"d": detail, "cr": detail.cr})
    finally:
        session.close()


# ---------------- JSON API ----------------


@app.get("/api/runs")
def api_runs():
    session = get_session()
    try:
        view = vm.build_index(session, include_empty=True)
        return [s.to_api() for s in reversed(view.summaries)]  # chronological, as before
    finally:
        session.close()


@app.get("/api/runs/{run_id}")
def api_run(run_id: int):
    session = get_session()
    try:
        detail = vm.build_run_detail(session, run_id)
        if detail is None:
            raise HTTPException(404, "run not found")
        out = detail.summary.to_api()
        out["cases"] = [
            {
                "id": r.summary.case_result_id,
                "case_id": v.case_id,
                "repetition": r.summary.repetition,
                "status": r.summary.status,
                "precision": r.summary.precision,
                "recall": r.summary.recall,
                "f1": r.summary.f1,
                "exec_overall": r.summary.exec_overall,
                "aggregate_band": r.summary.band or "",
                "assessment_id": r.summary.assessment_id,
                "assess_cost_usd": r.summary.costs.assess.usd,
                "assess_cost_reused": r.summary.costs.reused,
                "assess_time_s": r.summary.costs.assess_time_s,
                "judge_cost_usd": r.summary.costs.judge.usd,
                "judge_time_s": r.summary.costs.judge_time_s,
                "goldens": [
                    {"id": c.golden_id, "status": c.status}
                    for c in r.cells + r.retired_cells
                ],
            }
            for v in detail.vendors for r in v.reps
        ]
        return out
    finally:
        session.close()


@app.get("/api/compare")
def api_compare(runs: str | None = None, vendors: str | None = None):
    ids = _parse_run_ids(runs)
    session = get_session()
    try:
        _check_runs_exist(session, ids)
        return vm.matrix_to_api(vm.build_matrix(session, ids, _vendor_list(vendors)))
    finally:
        session.close()
