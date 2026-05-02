from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
