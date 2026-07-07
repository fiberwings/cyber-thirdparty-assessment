"""bench CLI: run benchmarks, list cases, init the results DB.

    bench run [--cases a,b] [--reps N] [--concurrency N]
              [--cleanup none|ok|all] [--judge-model M]
              [--override stage=model ...] [--smoke] [--notes "..."]
    bench list-cases
    bench init-db
"""

from __future__ import annotations

import argparse
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
        notes=args.notes or ("smoke" if args.smoke else ""),
    )
    run_id, status = run_batch(cases, config)
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
    p_run.add_argument("--judge-model", help=f"judge model (default {settings.JUDGE_MODEL})")
    p_run.add_argument("--override", action="append", metavar="STAGE=MODEL",
                       help="per-stage model override (repeatable)")
    p_run.add_argument("--smoke", action="store_true",
                       help="cheap end-to-end check: fast profile everywhere + cheap judge")
    p_run.add_argument("--notes", help="free-text note stored on the run")
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list-cases", help="list available cases")
    p_list.set_defaults(func=cmd_list_cases)

    p_init = sub.add_parser("init-db", help="create the results DB")
    p_init.set_defaults(func=cmd_init_db)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
