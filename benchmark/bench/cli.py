"""bench CLI: run benchmarks, list cases, init the results DB.

    bench run [--cases a,b] [--reps N] [--concurrency N]
              [--cleanup none|ok|all] [--judge none|match|full] [--judge-model M]
              [--skip-narratives] [--override stage=model ...] [--smoke]
              [--notes "..."]
    bench grade --assessment ID --case CASE [--judge none|match|full]
                [--judge-model M] [--notes "..."]      # grade a stored assessment, no pipeline
    bench list-cases
    bench init-db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import settings

OVERRIDE_STAGES = {
    "scoping", "scenarios", "gap_analysis", "weaknesses", "narrative",
    "executive_summary",
}


def _parse_overrides(pairs: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--override expects stage=model, got {pair!r}")
        stage, model = pair.split("=", 1)
        if stage not in OVERRIDE_STAGES:
            raise SystemExit(
                f"unknown stage {stage!r}; valid: {sorted(OVERRIDE_STAGES)}"
            )
        overrides[stage] = model
    return overrides


def _smoke_overrides() -> tuple[dict[str, str], str]:
    """All six stages pinned to the app's fast-profile default + a cheap judge."""
    from .app_client import AppClient

    client = AppClient()
    try:
        profiles = client.get_models()
    finally:
        client.close()
    fast = next((p["default_model"] for p in profiles if p["name"] == "fast"), None)
    if not fast:
        raise SystemExit("could not resolve the fast profile from GET /api/models")
    return {stage: fast for stage in OVERRIDE_STAGES}, fast


def cmd_run(args: argparse.Namespace) -> int:
    from .cases import CaseLoadError, load_cases
    from .runner import RunConfig, run_batch

    case_ids = args.cases.split(",") if args.cases else None
    try:
        cases = load_cases(Path(settings.CASES_DIR), case_ids)
    except CaseLoadError as e:
        print(f"case error: {e}", file=sys.stderr)
        return 2
    if not cases:
        print("no cases found", file=sys.stderr)
        return 2

    overrides = _parse_overrides(args.override or [])
    judge_model = args.judge_model
    if args.smoke:
        smoke_overrides, fast = _smoke_overrides()
        overrides = {**smoke_overrides, **overrides}
        judge_model = judge_model or fast
        print(f"smoke mode: all stages + judge pinned to {fast}")

    config = RunConfig(
        case_ids=case_ids,
        repetitions=args.reps,
        concurrency=args.concurrency,
        cleanup=args.cleanup,
        model_overrides=overrides,
        judge_model=judge_model,
        judge_mode=args.judge,
        skip_narratives=args.skip_narratives,
        allow_dev_cache=args.allow_dev_cache,
        notes=args.notes or ("smoke" if args.smoke else ""),
    )
    run_id, status = run_batch(cases, config)
    print(f"run {run_id} finished: {status}")
    return 0 if status == "done" else 1


def cmd_grade(args: argparse.Namespace) -> int:
    from .cases import CaseLoadError, load_cases
    from .runner import RunConfig, grade_stored

    try:
        cases = load_cases(Path(settings.CASES_DIR), [args.case])
    except CaseLoadError as e:
        print(f"case error: {e}", file=sys.stderr)
        return 2
    config = RunConfig(
        case_ids=[args.case],
        cleanup="none",
        judge_model=args.judge_model,
        judge_mode=args.judge,
        notes=args.notes or f"grade of stored assessment {args.assessment}",
    )
    run_id, status = grade_stored(cases[0], args.assessment, config)
    print(f"run {run_id} finished: {status}")
    return 0 if status == "done" else 1


def cmd_list_cases(_: argparse.Namespace) -> int:
    from .cases import CaseLoadError, load_cases

    try:
        cases = load_cases(Path(settings.CASES_DIR))
    except CaseLoadError as e:
        print(f"case error: {e}", file=sys.stderr)
        return 2
    for c in cases:
        n_docs = len(c.documents)
        n_gold = len(c.golden.expected_weaknesses)
        n_rubric = len(c.golden.exec_summary_rubric.must_cover)
        print(
            f"{c.id}: {c.vendor_name} — {n_docs} docs, "
            f"{n_gold} expected weaknesses, {n_rubric} must-cover points"
        )
    return 0


def cmd_recollect_tokens(args: argparse.Namespace) -> int:
    """Refresh stored tokens_json from the app's model_call table — e.g. after
    scripts/backfill_cost.py priced historical calls. Read-only on the app DB.

    `assessment_deleted` means the backend was asked to delete the assessment,
    not that its telemetry is gone: model_call rows often survive, so those case
    results are refreshed too and simply skipped when no rows are readable.
    """
    from sqlalchemy import select

    from . import collect
    from .db import get_session
    from .models import CaseResult

    session = get_session()
    try:
        q = select(CaseResult).where(CaseResult.assessment_id.is_not(None))
        if args.run is not None:
            q = q.where(CaseResult.run_id == args.run)
        updated = skipped = 0
        for cr in session.scalars(q.order_by(CaseResult.id)).all():
            tokens = collect.collect_model_calls(cr.assessment_id)
            if not tokens:
                skipped += 1
                continue
            cr.tokens_json = json.dumps(tokens)
            updated += 1
            print(
                f"run {cr.run_id} case {cr.case_id} r{cr.repetition} (assessment {cr.assessment_id}): "
                f"{tokens['total_calls']} calls, ${tokens['total_cost_usd']:.4f}"
                f"{' (estimated)' if tokens.get('total_cost_estimated') else ''}"
                f"{' ⚠ ' + str(tokens['total_cost_missing']) + ' unpriced' if tokens.get('total_cost_missing') else ''}"
            )
        session.commit()
    finally:
        session.close()
    print(f"updated {updated}, skipped {skipped} "
          f"(no model_call rows reachable at {settings.MAIN_DB_PATH})")
    return 0


def cmd_backfill_judge_cost(args: argparse.Namespace) -> int:
    """Price judge calls recorded before metered usage.cost capture, from
    current OpenRouter list pricing × recorded tokens. Metered rows untouched."""
    from sqlalchemy import select

    from .db import get_session
    from .models import JudgeCall
    from .pricing import estimate, fetch_pricing

    session = get_session()
    try:
        rows = session.scalars(
            select(JudgeCall).where(
                JudgeCall.cost_usd.is_(None),
                JudgeCall.ok.is_(True),
            )
        ).all()
        if not rows:
            print("nothing to backfill")
            return 0
        pricing = fetch_pricing()
        priced = unpriced = 0
        total = 0.0
        by_model: dict[str, list[int, float]] = {}
        for jc in rows:
            usd = estimate(jc.model_id, jc.input_tokens, jc.output_tokens, pricing)
            agg = by_model.setdefault(jc.model_id, [0, 0.0, 0])
            agg[0] += 1
            if usd is None:
                unpriced += 1
                agg[2] += 1
                continue
            agg[1] += usd
            total += usd
            priced += 1
            if args.apply:
                jc.cost_usd = usd
                jc.cost_source = "estimated"
        for model_id, (n, usd, miss) in sorted(by_model.items(), key=lambda kv: -kv[1][1]):
            note = f"  ({miss} unpriced — model delisted)" if miss else ""
            print(f"{model_id:45} {n:>5} calls  ${usd:>9.4f}{note}")
        print(f"\n{priced}/{len(rows)} judge calls priceable, total ≈ ${total:.4f}")
        if not args.apply:
            print("dry run — re-run with --apply to write")
            return 0
        session.commit()
        print(f"wrote {priced} rows to {settings.BENCH_DB_PATH}")
    finally:
        session.close()
    return 0


def cmd_cost(args: argparse.Namespace) -> int:
    """Per-run cost rollup: assessment pipeline + judge, for reconciliation
    against the OpenRouter bill.

    An assessment graded again (`bench grade`, or a re-run over a stored one)
    reports the same pipeline cost in every run that references it — real money
    was spent once. The per-run figures are as-run; the grand total counts each
    assessment once and marks re-reported ones `(reused)`.
    """
    from sqlalchemy import select

    from .costing import AssessmentLedger, Money, case_cost, judge_cost
    from .db import get_session
    from .models import CaseResult, JudgeCall, Run

    session = get_session()
    try:
        q = select(Run).order_by(Run.id)
        if args.run is not None:
            q = q.where(Run.id == args.run)
        print(f"{'run':>3} {'started':16} {'cases':>5} {'pipeline $':>11} {'judge $':>9} "
              f"{'total $':>9}  judge model")
        ledger = AssessmentLedger()          # pipeline spend, counted once per assessment
        grand_judge = Money()
        for run in session.scalars(q).all():
            results = session.scalars(
                select(CaseResult).where(CaseResult.run_id == run.id)
            ).all()
            pipe = Money()
            reused = 0
            for cr in results:
                if not cr.tokens_json:
                    continue
                cost = case_cost(json.loads(cr.tokens_json))
                pipe += cost
                reused += int(ledger.record(cr.assessment_id, cost))
            judge = judge_cost(
                session.execute(
                    select(JudgeCall.cost_usd, JudgeCall.cost_source, JudgeCall.ok,
                           JudgeCall.latency_ms)
                    .join(CaseResult, JudgeCall.case_result_id == CaseResult.id)
                    .where(CaseResult.run_id == run.id)
                ).all()
            )
            grand_judge += judge
            started = run.started_at.strftime("%Y-%m-%d %H:%M") if run.started_at else "—"
            flag = "~" if (pipe.estimated or judge.estimated) else " "
            note = f"  ({reused} reused)" if reused else ""
            pipe_usd, judge_usd = pipe.usd or 0.0, judge.usd or 0.0
            print(f"{run.id:>3} {started:16} {len(results):>5} {pipe_usd:>11.4f} {judge_usd:>9.4f} "
                  f"{flag}{pipe_usd + judge_usd:>8.4f}  {run.judge_model}{note}")

        pipe_total = ledger.total()
        pipe_usd, judge_usd = pipe_total.usd or 0.0, grand_judge.usd or 0.0
        print(f"\nDistinct assessments: {ledger.distinct} → pipeline ${pipe_usd:.4f}")
        print(f"Judge (every call, never reused):  ${judge_usd:.4f}")
        print(f"Spend attributable to this tool:   ${pipe_usd + judge_usd:.4f}")
        if pipe_total.missing or grand_judge.missing:
            print(f"⚠ excludes {pipe_total.missing} pipeline + {grand_judge.missing} judge call(s) "
                  "with no price (delisted model, or cost never reported)")
        print("`~` = contains list-price estimates, not metered OpenRouter cost.")
        print("Compare with https://openrouter.ai/activity over the same window — that "
              "bill also covers calls made outside this tool (manual runs, the app UI).")
    finally:
        session.close()
    return 0


def cmd_init_db(_: argparse.Namespace) -> int:
    from .db import get_engine

    get_engine()
    print(f"results DB ready at {settings.BENCH_DB_PATH}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="bench", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run the benchmark batch")
    p_run.add_argument("--cases", help="comma-separated case ids (default: all)")
    p_run.add_argument("--reps", type=int, default=1, help="repetitions per case")
    p_run.add_argument("--concurrency", type=int, default=1,
                       help="parallel cases (experimental above 1; keep <=3)")
    p_run.add_argument("--cleanup", choices=["none", "ok", "all"], default="ok",
                       help="delete created assessments: ok=successful only (default)")
    p_run.add_argument("--judge", choices=["none", "match", "full"], default="full",
                       help="none=no judge calls (band error + counts only); "
                            "match=weakness matching (P/R/F1); "
                            "full=matching + signal/noise classification + exec rubric (default)")
    p_run.add_argument("--skip-narratives", action="store_true",
                       help="skip the narratives + executive-summary stage (~30k tokens); "
                            "the exec rubric is then not graded")
    p_run.add_argument("--allow-dev-cache", action="store_true",
                       help="do not refuse a backend whose dev LLM cache is on (plumbing runs only)")
    p_run.add_argument("--judge-model", help=f"judge model (default {settings.JUDGE_MODEL})")
    p_run.add_argument("--override", action="append", metavar="STAGE=MODEL",
                       help="per-stage model override (repeatable)")
    p_run.add_argument("--smoke", action="store_true",
                       help="cheap end-to-end check: fast profile everywhere + cheap judge")
    p_run.add_argument("--notes", help="free-text note stored on the run")
    p_run.set_defaults(func=cmd_run)

    p_grade = sub.add_parser("grade", help="grade an assessment already on the backend (no pipeline, no app LLM cost)")
    p_grade.add_argument("--assessment", type=int, required=True, help="assessment id on the backend")
    p_grade.add_argument("--case", required=True, help="case id whose golden key to grade against")
    p_grade.add_argument("--judge", choices=["none", "match", "full"], default="full")
    p_grade.add_argument("--judge-model", help=f"judge model (default {settings.JUDGE_MODEL})")
    p_grade.add_argument("--notes", help="free-text note stored on the run")
    p_grade.set_defaults(func=cmd_grade)

    p_list = sub.add_parser("list-cases", help="list available cases")
    p_list.set_defaults(func=cmd_list_cases)

    p_recollect = sub.add_parser(
        "recollect-tokens",
        help="refresh stored token/cost telemetry from the app DB (e.g. after backfill_cost.py)",
    )
    p_recollect.add_argument("--run", type=int, help="limit to one run id (default: all)")
    p_recollect.set_defaults(func=cmd_recollect_tokens)

    p_backfill = sub.add_parser(
        "backfill-judge-cost",
        help="price historical judge calls from OpenRouter list pricing (dry run by default)",
    )
    p_backfill.add_argument("--apply", action="store_true", help="write estimates")
    p_backfill.set_defaults(func=cmd_backfill_judge_cost)

    p_cost = sub.add_parser("cost", help="per-run pipeline + judge cost rollup")
    p_cost.add_argument("--run", type=int, help="limit to one run id")
    p_cost.set_defaults(func=cmd_cost)

    p_init = sub.add_parser("init-db", help="create the results DB")
    p_init.set_defaults(func=cmd_init_db)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
