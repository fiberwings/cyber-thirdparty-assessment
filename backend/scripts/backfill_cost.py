"""Backfill `model_call.cost_usd` for rows recorded before OpenRouter's metered
`usage.cost` was captured, using *current* list pricing × recorded tokens.

    estimate = input_tokens × pricing.prompt + output_tokens × pricing.completion

The estimate assumes pricing has not changed since the call was made and that
no provider prompt-cache discount applied (historical rows have no
cached_tokens, so this is an upper bound for cached-heavy calls). Rows are
labelled `cost_source = "estimated"` so the benchmark dashboard can tell them
apart from metered figures; metered rows are never touched.

Usage (from `backend/`):
    .venv/bin/python scripts/backfill_cost.py            # dry run: per-model summary
    .venv/bin/python scripts/backfill_cost.py --apply    # write estimates
    .venv/bin/python scripts/backfill_cost.py --db ../data/tprm.sqlite --apply
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import httpx

MODELS_URL = "https://openrouter.ai/api/v1/models"

# Only rows that were really billed and never priced: live (not dev-cache),
# with tokens, and with no metered or prior estimated cost. Azure calls
# (`azure:` / `foundry:` refs) are unmetered by design and have no OpenRouter
# list price — they are left alone.
_CANDIDATES_SQL = """
SELECT id, model_id, input_tokens, output_tokens
FROM model_call
WHERE COALESCE(cost_usd, 0) = 0
  AND COALESCE(cost_source, '') = ''
  AND NOT COALESCE(cached, 0)
  AND (COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)) > 0
  AND model_id NOT LIKE 'azure:%'
  AND model_id NOT LIKE 'foundry:%'
"""


@dataclass(frozen=True)
class Price:
    prompt: float  # USD per input token
    completion: float  # USD per output token


def fetch_pricing() -> dict[str, Price]:
    resp = httpx.get(MODELS_URL, timeout=30)
    resp.raise_for_status()
    out: dict[str, Price] = {}
    for m in resp.json()["data"]:
        p = m.get("pricing") or {}
        try:
            out[m["id"]] = Price(float(p.get("prompt") or 0), float(p.get("completion") or 0))
        except (TypeError, ValueError):
            continue
    return out


def estimate(rows: list[tuple[int, str, int, int]], pricing: dict[str, Price]) -> tuple[dict[int, float], dict[str, dict]]:
    """Returns (row_id → estimated cost, per-model summary incl. unpriced models)."""
    costs: dict[int, float] = {}
    summary: dict[str, dict] = defaultdict(lambda: {"rows": 0, "in": 0, "out": 0, "usd": 0.0, "priced": False})
    for row_id, model_id, in_tok, out_tok in rows:
        s = summary[model_id]
        s["rows"] += 1
        s["in"] += in_tok or 0
        s["out"] += out_tok or 0
        # `openrouter:` refs price under their bare id.
        price = pricing.get(model_id.removeprefix("openrouter:"))
        if price is None:
            continue
        s["priced"] = True
        usd = (in_tok or 0) * price.prompt + (out_tok or 0) * price.completion
        s["usd"] += usd
        costs[row_id] = usd
    return costs, dict(summary)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, help="app SQLite path (default: DB_PATH from settings)")
    ap.add_argument("--apply", action="store_true", help="write estimates (default: dry run)")
    args = ap.parse_args()

    if args.db is None:
        from app.config import settings

        args.db = settings.db_path
    if not args.db.exists():
        print(f"no DB at {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(model_call)")}
    if "cost_source" not in cols:
        print("model_call.cost_source is missing — start the app once to run migrations.", file=sys.stderr)
        return 2

    rows = conn.execute(_CANDIDATES_SQL).fetchall()
    if not rows:
        print("nothing to backfill")
        return 0
    pricing = fetch_pricing()
    costs, summary = estimate(rows, pricing)

    print(f"{'model':45} {'rows':>5} {'in_tok':>10} {'out_tok':>10} {'est_usd':>10}")
    for model_id, s in sorted(summary.items(), key=lambda kv: -kv[1]["usd"]):
        usd = f"{s['usd']:.4f}" if s["priced"] else "unpriced"
        print(f"{model_id:45} {s['rows']:>5} {s['in']:>10} {s['out']:>10} {usd:>10}")
    total = sum(costs.values())
    print(f"\n{len(costs)}/{len(rows)} rows priceable, total ≈ ${total:.4f}")

    if not args.apply:
        print("dry run — re-run with --apply to write")
        return 0
    with conn:
        conn.executemany(
            "UPDATE model_call SET cost_usd = ?, cost_source = 'estimated' WHERE id = ?",
            [(usd, row_id) for row_id, usd in costs.items()],
        )
    print(f"wrote {len(costs)} rows to {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
