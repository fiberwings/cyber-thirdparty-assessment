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
    JUDGE_MAX_TOKENS: int = 8192

    # Results DB
    BENCH_DB_PATH: str = str(BENCHMARK_DIR / "data" / "bench.sqlite")

    # Cases
    CASES_DIR: str = str(BENCHMARK_DIR / "cases")

    # Provenance / optional collection (main repo assumed co-located)
    MAIN_REPO_DIR: str = str(BENCHMARK_DIR.parent)
    MAIN_DB_PATH: str = str(BENCHMARK_DIR.parent / "data" / "tprm.sqlite")

    # Stage timeouts (seconds)
    TIMEOUT_SCENARIOS: int = 600
    TIMEOUT_EXTRACTION: int = 600
    TIMEOUT_CORRELATE: int = 600
    TIMEOUT_GAP_ANALYSIS: int = 1800
    TIMEOUT_NARRATIVES: int = 900
    POLL_INTERVAL: float = 2.0

    # Judge match confidence levels that count as a true positive
    MATCH_CONFIDENCE_THRESHOLD: tuple[str, ...] = ("high", "medium")


settings = BenchSettings()
