"""Optional, best-effort token/latency/cost collection.

Reads the main app's model_call table read-only (no main-app code import, no
writes). Disabled automatically when the DB isn't reachable — e.g. a remote
backend — in which case per-case tokens are simply absent.

cost_usd comes from OpenRouter's `usage.cost` as recorded by the app. A live,
successful call that recorded no cost is counted in `cost_missing` so a $0 total
is never mistaken for a free run; rows backfilled from list pricing are counted
in `cost_estimated` so estimates are distinguishable from metered figures.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import settings

# Columns added after the first model_call schema; selected as 0 when the app DB
# predates them so an older DB still yields tokens/cost instead of nothing.
_OPTIONAL_COLUMNS = ("cached", "cached_tokens", "reasoning_tokens", "cost_usd", "cost_source")


def _col(name: str, present: set[str]) -> str:
    return f"COALESCE({name}, 0)" if name in present else "0"


def _estimated(present: set[str]) -> str:
    if "cost_source" not in present:
        return "0"
    return "CASE WHEN cost_source = 'estimated' THEN 1 ELSE 0 END"


def collect_model_calls(assessment_id: int, main_db_path: str | None = None) -> dict | None:
    path = Path(main_db_path or settings.MAIN_DB_PATH)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            present = {r[1] for r in conn.execute("PRAGMA table_info(model_call)")}
            if not present:
                return None
            rows = conn.execute(
                f"""
                SELECT purpose, model_id,
                       COUNT(*) as calls,
                       SUM(COALESCE(input_tokens, 0)) as input_tokens,
                       SUM(COALESCE(output_tokens, 0)) as output_tokens,
                       SUM({_col("cached_tokens", present)}) as cached_tokens,
                       SUM({_col("reasoning_tokens", present)}) as reasoning_tokens,
                       SUM(COALESCE(latency_ms, 0)) as latency_ms,
                       SUM({_col("cost_usd", present)}) as cost_usd,
                       SUM(CASE WHEN ok THEN 0 ELSE 1 END) as errors,
                       SUM(CASE WHEN ok AND NOT {_col("cached", present)}
                                     AND {_col("cost_usd", present)} = 0
                                THEN 1 ELSE 0 END) as cost_missing,
                       SUM({_estimated(present)}) as cost_estimated
                FROM model_call
                WHERE assessment_id = ?
                GROUP BY purpose, model_id
                """,
                (assessment_id,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    by_purpose = [
        {
            "purpose": r[0],
            "model_id": r[1],
            "calls": r[2],
            "input_tokens": r[3],
            "output_tokens": r[4],
            "cached_tokens": r[5],
            "reasoning_tokens": r[6],
            "latency_ms": r[7],
            "cost_usd": r[8],
            "errors": r[9],
            "cost_missing": r[10],
            # rows priced by scripts/backfill_cost.py (list price × tokens), not metered
            "cost_estimated": r[11],
        }
        for r in rows
    ]
    return {
        "by_purpose": by_purpose,
        "total_input_tokens": sum(r["input_tokens"] for r in by_purpose),
        "total_output_tokens": sum(r["output_tokens"] for r in by_purpose),
        "total_cached_tokens": sum(r["cached_tokens"] for r in by_purpose),
        "total_reasoning_tokens": sum(r["reasoning_tokens"] for r in by_purpose),
        "total_cost_usd": round(sum(r["cost_usd"] for r in by_purpose), 6),
        "total_cost_missing": sum(r["cost_missing"] for r in by_purpose),
        "total_cost_estimated": sum(r["cost_estimated"] for r in by_purpose),
        "total_calls": sum(r["calls"] for r in by_purpose),
    }
