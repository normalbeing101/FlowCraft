"""Deployment configuration (env-driven, validated, no secrets in workflows).

Categories kept separate:
  1. Deployment config      -> this module (env vars, FLOWCRAFT_ prefix)
  2. Provider credentials   -> env var *names* referenced by provider profiles
  3. Workflow definitions   -> YAML/JSON files or DB records
  4. Tool settings          -> per-step `config` + deployment limits below
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FLOWCRAFT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- core ---
    app_name: str = "FlowCraft"
    database_url: str = "sqlite+aiosqlite:///./flowcraft.db"
    api_auth_token: str = ""          # empty => auth disabled (single-user dev mode)
    workspace_id: str = "default"     # MVP: single workspace; isolation enforced on reads
    log_level: str = "INFO"

    # --- execution policy bounds (deployment caps; workflows may request less) ---
    max_run_seconds_cap: int = Field(default=3600, ge=1)
    max_step_timeout_cap: float = Field(default=900.0, gt=0)
    default_provider_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    # --- HTTP tool network policy (SSRF) ---
    http_allow_private: bool = False
    http_allow_localhost: bool = False
    http_allowed_hosts: str = ""      # comma-separated; empty = any public host
    http_denied_hosts: str = ""       # comma-separated patterns (e.g. *.internal)
    http_max_response_bytes: int = Field(default=2_000_000, ge=1024)

    # --- history retention ---
    run_retention_days: int = Field(default=90, ge=1)

    @field_validator("log_level")
    @classmethod
    def _lvl(cls, v: str) -> str:
        v = v.upper()
        assert v in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}, v
        return v

    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.http_allowed_hosts.split(",") if h.strip()]

    def denied_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.http_denied_hosts.split(",") if h.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
