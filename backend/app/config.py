from __future__ import annotations

from pathlib import Path

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
    # large = dense synthesis over many items). Lowering any of these below
    # the shipped defaults is a flagged accuracy regression — see CLAUDE.md.
    llm_budget_small: int = Field(default=4096, alias="LLM_BUDGET_SMALL")
    llm_budget_medium: int = Field(default=8192, alias="LLM_BUDGET_MEDIUM")
    llm_budget_large: int = Field(default=16384, alias="LLM_BUDGET_LARGE")
    # Ceiling for the automatic retry-with-more-tokens on truncated output,
    # and how many doublings the ladder may take before failing loudly.
    llm_truncation_cap: int = Field(default=32768, alias="LLM_TRUNCATION_CAP")
    llm_truncation_retries: int = Field(default=2, alias="LLM_TRUNCATION_RETRIES")
    # Per-attempt HTTP timeout scales with the requested output budget:
    # base + tokens / assumed_tps, clamped to timeout_max. The client is
    # non-streaming, so the read timeout must cover the whole generation.
    llm_timeout_base_s: float = Field(default=60.0, alias="LLM_TIMEOUT_BASE_S")
    llm_assumed_output_tps: float = Field(default=40.0, alias="LLM_ASSUMED_OUTPUT_TPS")
    llm_timeout_max_s: float = Field(default=600.0, alias="LLM_TIMEOUT_MAX_S")
    # Minimum model output cap a reasoner-profile model must support — the
    # truncation ladder can request up to llm_truncation_cap tokens.
    llm_min_model_output_cap: int = Field(default=32768, alias="LLM_MIN_MODEL_OUTPUT_CAP")

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
