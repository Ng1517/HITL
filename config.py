"""
config.py
---------
All configuration is read from environment variables (see .env.example).
No credentials or secrets are hard-coded anywhere in this codebase.
"""

from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- General ---
    public_base_url: str = Field(
        default="http://localhost:8000",
        description="Public URL the approval service is reachable at. "
        "MUST be https:// in production.",
    )

    database_url: str = Field(
        default="sqlite:///./approvals.db"
    )

    internal_api_key: str = Field(
        ...,
        description="Shared secret the Langflow component uses to call "
        "the internal /internal/* endpoints.",
    )

    default_ttl_minutes: int = Field(default=1440)
    log_level: str = Field(default="INFO")

    # --- Rate limiting ---
    rate_limit_per_minute: int = Field(default=20)

    # --- Email ---
    email_provider: str = Field(default="smtp")

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    sender_email: str | None = None
    sender_name: str = "Langflow Approvals"

    # --- API email providers ---
    resend_api_key: str | None = None
    sendgrid_api_key: str | None = None

    # --- Continuation webhook ---
    langflow_continuation_enabled: bool = Field(default=False)

    langflow_api_url: str | None = Field(
        default=None,
        description="Langflow continuation URL",
    )

    langflow_api_key: str | None = None


@lru_cache
def _get_settings() -> Settings:
    return Settings()


settings = _get_settings()