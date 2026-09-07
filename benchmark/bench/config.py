"""Benchmark settings.

Everything is overridable via environment variables or benchmark/.env.
The benchmark never imports main-app code; MAIN_REPO_DIR / MAIN_DB_PATH are
used only for provenance capture (git SHA) and optional read-only token
collection from the main app's model_call table.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BENCHMARK_DIR = Path(__file__).resolve().parent.parent


class BenchSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BENCHMARK_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Main app under test
    BENCH_BACKEND_URL: str = "http://localhost:8000"

    # Judge (direct OpenRouter, independent of the main app)
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    JUDGE_MODEL: str = "anthropic/claude-sonnet-4.6"
    # Sized for 100+ reported findings: the match and classification steps
    # emit one justified entry per finding (~100 tokens each with a verbose
    # judge) and the baseline already hit 8192 at ~40 findings.
    JUDGE_MAX_TOKENS: int = 32768
    # Finding classification (judge=full): the whole evidence bundle (all
    # chunks) is sent when it fits this many characters (~4 chars/token);
    # otherwise only the chunks the reported weaknesses cite are sent.
    JUDGE_CLASSIFY_CHUNK_BUDGET_CHARS: int = 120_000

    # Results DB
    BENCH_DB_PATH: str = str(BENCHMARK_DIR / "data" / "bench.sqlite")

    # Cases
    CASES_DIR: str = str(BENCHMARK_DIR / "cases")

    # Provenance / optional collection (main repo assumed co-located)
    MAIN_REPO_DIR: str = str(BENCHMARK_DIR.parent)
    MAIN_DB_PATH: str = str(BENCHMARK_DIR.parent / "data" / "tprm.sqlite")

    # Liveness-based stage waits (seconds). A stage fails only when the backend
    # task shows no activity (last_activity_at / progress / detail unchanged)
    # for IDLE_TIMEOUT_S, or when it exceeds the MAX_STAGE_S runaway ceiling —
    # never because a slow model took long while producing tokens. Keep
    # IDLE_TIMEOUT_S above the backend's TASK_IDLE_TIMEOUT_S (600) so the
    # harness records the backend's own diagnostic rather than its own.
    IDLE_TIMEOUT_S: float = 900.0
    MAX_STAGE_S: float = 14400.0
    POLL_INTERVAL: float = 2.0

    # Judge match confidence levels that count as a true positive
    MATCH_CONFIDENCE_THRESHOLD: tuple[str, ...] = ("high", "medium")


settings = BenchSettings()
