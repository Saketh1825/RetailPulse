"""Central configuration. All settings come from environment variables (or a local .env file).

Nothing secret is hard-coded anywhere in the repository. Secrets are wrapped in SecretStr so they
are masked if a Settings object is ever printed or logged.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", extra="ignore")

    # --- databases -------------------------------------------------------------------------
    # Read/write connection: used by the ETL, the forecast job and the normal API endpoints.
    database_url: str = "postgresql://retailpulse:retailpulse@localhost:5432/retailpulse"
    # SELECT-only connection: used ONLY by the natural-language query feature.
    readonly_database_url: str = (
        "postgresql://retailpulse_readonly:readonly_dev_password@localhost:5432/retailpulse"
    )

    # --- LLM (any OpenAI-compatible chat-completions endpoint, e.g. Groq or Gemini) ----------
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = "llama-3.3-70b-versatile"
    llm_timeout_seconds: float = 20.0
    llm_max_output_tokens: int = 400

    # --- NL-to-SQL safety limits ----------------------------------------------------------------
    nl_max_rows: int = Field(200, ge=1, le=1000)
    nl_statement_timeout_ms: int = Field(5000, ge=100, le=60000)
    nl_question_max_chars: int = Field(300, ge=20, le=2000)

    # --- API hardening ----------------------------------------------------------------------------
    api_key: SecretStr | None = None            # if set, POST /query requires X-API-Key
    query_rate_limit_per_minute: int = Field(20, ge=1)
    cors_allow_origins: str = ""                # comma-separated; empty = same-origin only

    @field_validator("llm_api_key", "api_key", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, v):
        return None if v is None or (isinstance(v, str) and not v.strip()) else v

    # --- ETL rules ---------------------------------------------------------------------------------
    etl_min_date: str = "2015-01-01"
    etl_units_outlier_factor: float = 10.0      # units > factor * item median -> rejected
    etl_price_outlier_low: float = 0.25         # unit price < low * item median -> rejected
    etl_price_outlier_high: float = 4.0         # unit price > high * item median -> rejected
    rejected_dir: Path = PROJECT_ROOT / "data" / "rejected"

    # --- forecasting -------------------------------------------------------------------------------
    forecast_horizon_days: int = Field(28, ge=7, le=90)
    model_dir: Path = PROJECT_ROOT / "artifacts" / "models"
    forecast_min_history_days: int = 120


@lru_cache
def get_settings() -> Settings:
    return Settings()
