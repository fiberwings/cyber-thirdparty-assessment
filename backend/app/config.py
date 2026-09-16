from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env candidate paths absolutely so it doesn't matter whether
# uvicorn was launched from the repo root or from backend/.
#   backend/app/config.py  →  parents[0]=app, [1]=backend, [2]=repo-root
_HERE = Path(__file__).resolve()
_BACKEND_DIR = _HERE.parents[1]
_REPO_ROOT = _HERE.parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # First match wins. Repo root takes precedence so the `.env` lives
        # next to docker-compose.yml as the README documents.
        env_file=(
            _REPO_ROOT / ".env",
            _BACKEND_DIR / ".env",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
        # Disable Pydantic's "model_*" protected namespace — we use it for
        # OpenRouter model IDs (model_fast, model_reasoner, ...).
        protected_namespaces=(),
    )

    # OpenRouter
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1",
        alias="OPENROUTER_BASE_URL",
    )
    openrouter_referer: str = Field(
        default="http://localhost:3000",
        alias="OPENROUTER_REFERER",
    )
    openrouter_app_name: str = Field(
        default="cyber-tprm-assessment",
        alias="OPENROUTER_APP_NAME",
    )
    # Comma-separated OpenRouter provider names never to route to (sent as
    # `provider.ignore`). OpenRouter load-balances one model id across many
    # hosts; a host whose content filter stops generation on security or
    # jurisdiction text (StreamLake on glm-5.3-flash, 2026-09: native finish
    # reason `sensitive`, torn JSON reported as `stop`) makes assessments fail
    # or — worse — lose findings. Empty = OpenRouter's default routing.
    openrouter_provider_ignore: str = Field(default="", alias="OPENROUTER_PROVIDER_IGNORE")

    # Default model profiles (overridable per call)
    model_fast: str = Field(default="anthropic/claude-haiku-4.5", alias="MODEL_FAST")
    model_reasoner: str = Field(default="anthropic/claude-opus-4.7", alias="MODEL_REASONER")
    # Selectable alternatives shown in the UI ModelPicker
    model_reasoner_alternatives: str = Field(
        default="anthropic/claude-opus-4.7,openai/gpt-5,anthropic/claude-sonnet-4.6",
        alias="MODEL_REASONER_ALTERNATIVES",
    )
    model_fast_alternatives: str = Field(
        default="anthropic/claude-haiku-4.5,openai/gpt-5-mini",
        alias="MODEL_FAST_ALTERNATIVES",
    )

    # Storage
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    storage_dir: Path = Field(default=Path("./storage"), alias="STORAGE_DIR")
    db_path: Path = Field(default=Path("./data/tprm.sqlite"), alias="DB_PATH")

    # CORS / dev
    cors_origins: str = Field(default="http://localhost:3000", alias="CORS_ORIGINS")

    # HMAC for storage URLs
    storage_secret: str = Field(default="dev-secret-change-me", alias="STORAGE_SECRET")

    # Tunables
    fts_topk: int = Field(default=8, alias="FTS_TOPK")
    max_upload_mb: int = Field(default=50, alias="MAX_UPLOAD_MB")

    # LLM output-token budget policy. Three tiers, sized per call kind
    # (small = short structured outputs, medium = single-item detail work,
    # large = dense synthesis over many items). The budgets bound *hidden
    # reasoning too*: providers count thinking tokens against max_tokens, and
    # the reasoners in use spend 70-85 % of their output on it, so a tier must
    # leave room for the model to think before it writes (2026-09 matrix:
    # ~10k reasoning tokens per extraction call). Lowering any of these below
    # the shipped defaults is a flagged accuracy regression — see CLAUDE.md.
    llm_budget_small: int = Field(default=16384, alias="LLM_BUDGET_SMALL")
    llm_budget_medium: int = Field(default=32768, alias="LLM_BUDGET_MEDIUM")
    llm_budget_large: int = Field(default=65536, alias="LLM_BUDGET_LARGE")
    # Ceiling for the automatic retry-with-more-tokens on truncated output,
    # and how many doublings the ladder may take before failing loudly. The
    # ceiling is deliberately below every catalogued model's output cap
    # (128 000 = Claude Opus/Sonnet; glm-5.3-flash is 131 072, DeepSeek 384 000)
    # and is the only runaway guard on a looping generation — do not set it to
    # a model's absolute maximum.
    llm_truncation_cap: int = Field(default=128_000, alias="LLM_TRUNCATION_CAP")
    llm_truncation_retries: int = Field(default=2, alias="LLM_TRUNCATION_RETRIES")
    # Liveness-based limits (the client streams completions, so it observes
    # progress instead of guessing a duration — any model speed works):
    #   connect  — TCP/TLS connect + pool acquisition
    #   idle     — tier (i): no bytes at all on the socket (OpenRouter keepalive
    #              comments count as bytes) → dead connection
    #   silence  — tier (ii): no output or reasoning token for this long even
    #              though the connection is alive. Models that reason *hidden*
    #              (no reasoning deltas on the wire) look identical to a stuck
    #              generation here — glm-5.3-flash thought for > 15 min before
    #              its first visible token on a 32k budget — so the default
    #              equals the ceiling (tier off); tighten it only for models
    #              that stream their reasoning or do not reason.
    #   call max — hard per-attempt ceiling, a runaway guard sized in hours
    # Lowering silence / call max below the shipped defaults re-introduces
    # speed-based failures on slow reasoners — an accuracy trade-off (CLAUDE.md).
    llm_connect_timeout_s: float = Field(default=30.0, alias="LLM_CONNECT_TIMEOUT_S")
    llm_stream_idle_s: float = Field(default=180.0, alias="LLM_STREAM_IDLE_S")
    llm_content_silence_s: float = Field(default=3600.0, alias="LLM_CONTENT_SILENCE_S")
    llm_call_max_s: float = Field(default=3600.0, alias="LLM_CALL_MAX_S")
    # Background-task watchdog: a task with no activity ping (streamed token,
    # keepalive, progress update) for idle_timeout, or older than max_runtime,
    # is cancelled with a diagnostic error instead of blocking the assessment
    # forever. idle_timeout must exceed llm_stream_idle_s so the router reports
    # a dead socket before the watchdog fires.
    task_idle_timeout_s: float = Field(default=600.0, alias="TASK_IDLE_TIMEOUT_S")
    task_max_runtime_s: float = Field(default=14400.0, alias="TASK_MAX_RUNTIME_S")
    task_watchdog_interval_s: float = Field(default=15.0, alias="TASK_WATCHDOG_INTERVAL_S")
    task_activity_persist_s: float = Field(default=10.0, alias="TASK_ACTIVITY_PERSIST_S")
    # Minimum model output cap a reasoner-profile model must support — the
    # truncation ladder can request up to llm_truncation_cap tokens.
    llm_min_model_output_cap: int = Field(default=128_000, alias="LLM_MIN_MODEL_OUTPUT_CAP")
    # Failure forensics: when set, every failed LLM call writes one JSON file
    # (all attempts: request budget, finish reasons, provider, usage and the
    # complete model output) into this directory. Off by default; failures are
    # always summarised on model_call regardless.
    llm_failure_dump_dir: Optional[Path] = Field(default=None, alias="LLM_FAILURE_DUMP_DIR")
    # Root log level for the app's own loggers (uvicorn keeps its own config).
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def provider_ignore_list(self) -> list[str]:
        return [p.strip() for p in self.openrouter_provider_ignore.split(",") if p.strip()]

    @model_validator(mode="after")
    def _validate_budget_policy(self) -> "Settings":
        if not (
            0
            < self.llm_budget_small
            <= self.llm_budget_medium
            <= self.llm_budget_large
            <= self.llm_truncation_cap
            <= self.llm_min_model_output_cap
        ):
            raise ValueError(
                "LLM budget policy must satisfy 0 < LLM_BUDGET_SMALL <= LLM_BUDGET_MEDIUM "
                "<= LLM_BUDGET_LARGE <= LLM_TRUNCATION_CAP <= LLM_MIN_MODEL_OUTPUT_CAP"
            )
        if self.llm_truncation_retries < 0:
            raise ValueError("LLM_TRUNCATION_RETRIES must be >= 0")
        if not (
            0
            < self.llm_connect_timeout_s
            <= self.llm_stream_idle_s
            <= self.llm_content_silence_s
            <= self.llm_call_max_s
        ):
            raise ValueError(
                "Liveness policy must satisfy 0 < LLM_CONNECT_TIMEOUT_S <= LLM_STREAM_IDLE_S "
                "<= LLM_CONTENT_SILENCE_S <= LLM_CALL_MAX_S"
            )
        if self.task_idle_timeout_s < self.llm_stream_idle_s:
            raise ValueError("TASK_IDLE_TIMEOUT_S must be >= LLM_STREAM_IDLE_S")
        if self.task_max_runtime_s < self.llm_call_max_s:
            raise ValueError("TASK_MAX_RUNTIME_S must be >= LLM_CALL_MAX_S")
        if self.task_watchdog_interval_s <= 0 or self.task_activity_persist_s < 0:
            raise ValueError("TASK_WATCHDOG_INTERVAL_S must be > 0 and TASK_ACTIVITY_PERSIST_S >= 0")
        return self

    # Deployment environment: "dev" | "production". Some dev-only switches
    # (LLM response cache) are refused outright in production.
    app_env: str = Field(default="dev", alias="APP_ENV")
    # Dev-only LLM response cache keyed by (model, messages, sampling params).
    # Off by default. Serves a stored response instead of calling the model so
    # unchanged pipeline stages cost nothing while iterating on other stages.
    # NEVER active in production; the benchmark refuses to run against a
    # backend that has it on (see benchmark README).
    llm_dev_cache: bool = Field(default=False, alias="LLM_DEV_CACHE")

    @property
    def llm_dev_cache_active(self) -> bool:
        return bool(self.llm_dev_cache) and self.app_env.lower() != "production"

    @property
    def reasoner_alternatives(self) -> list[str]:
        return [m.strip() for m in self.model_reasoner_alternatives.split(",") if m.strip()]

    @property
    def fast_alternatives(self) -> list[str]:
        return [m.strip() for m in self.model_fast_alternatives.split(",") if m.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.storage_dir.mkdir(parents=True, exist_ok=True)
