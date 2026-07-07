"""Optional, best-effort token/latency/cost collection.

Reads the main app's model_call table read-only (no main-app code import, no
writes). Disabled automatically when the DB isn't reachable — e.g. a remote
backend — in which case per-case tokens are simply absent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import settings


def collect_model_calls(assessment_id: int, main_db_path: str | None = None) -> dict | None:
    path = Path(main_db_path or settings.MAIN_DB_PATH)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                """
                SELECT purpose, model_id,
                       COUNT(*) as calls,
                       SUM(COALESCE(input_tokens, 0)) as input_tokens,
                       SUM(COALESCE(output_tokens, 0)) as output_tokens,
                       SUM(COALESCE(latency_ms, 0)) as latency_ms,
                       SUM(COALESCE(cost_usd, 0)) as cost_usd,
                       SUM(CASE WHEN ok THEN 0 ELSE 1 END) as errors
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
            "latency_ms": r[5],
            "cost_usd": r[6],
            "errors": r[7],
        }
        for r in rows
    ]
    return {
        "by_purpose": by_purpose,
        "total_input_tokens": sum(r["input_tokens"] for r in by_purpose),
        "total_output_tokens": sum(r["output_tokens"] for r in by_purpose),
        "total_cost_usd": round(sum(r["cost_usd"] for r in by_purpose), 6),
        "total_calls": sum(r["calls"] for r in by_purpose),
    }
