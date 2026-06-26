"""Configuration via Pydantic Settings.

Loaded once at startup and cached via `lru_cache`. Fail-fast on missing
secrets — refuses to start the app if `JWT_SECRET` or `DB_*` are placeholders.

Sources (in order, last wins):
  1. `.env.local` file (git-ignored, dev only).
  2. Process environment (Azure App Service Configuration in prod).
"""
from __future__ import annotations

import re
from functools import lru_cache

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_TOKENS = {
    "replace-me",
    "replace-me-with-48-bytes-of-random-base64",
    "change-me",
    "dev-secret-change-me-for-prod",
    "",
}


class Settings(BaseSettings):
    """All runtime configuration. Frozen after instantiation."""

    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- Auth (required) ----
    jwt_secret: SecretStr = Field(default=SecretStr(""))
    jwt_ttl_hours: int = 12
    # Secure-by-default: the session cookie is only sent over HTTPS. Local dev
    # over plain http must set COOKIE_SECURE=false (otherwise the browser drops it).
    cookie_secure: bool = True

    # ---- DB Azure SQL (required) ----
    db_server: str = ""
    db_name: str = ""
    db_user: str = ""
    db_password: SecretStr = Field(default=SecretStr(""))
    db_driver: str = "ODBC Driver 18 for SQL Server"
    db_schema: str = "fpoc"

    # ---- Twilio ----
    twilio_account_sid: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: SecretStr = Field(default=SecretStr(""))
    twilio_auth_token: SecretStr = Field(default=SecretStr(""))
    twilio_whatsapp_from: str = "whatsapp:+56957018982"
    twilio_inbound_public_url: str = ""
    twilio_inbound_validate_signature: bool = True

    # ---- Azure OpenAI ----
    azure_openai_endpoint: str = ""
    azure_openai_api_key: SecretStr = Field(default=SecretStr(""))
    azure_openai_chat_deployment: str = "gpt-4o-mini"
    azure_openai_api_version: str = "2024-12-01-preview"

    # ---- Azure Storage (documents) ----
    azure_storage_connection_string: str = ""
    azure_storage_container: str = "td-documents"

    # ---- Behavior ----
    notifications_dry_run: bool = True
    notifications_enabled: bool = True
    cors_allowed_origins: str = "http://localhost:5180"
    initial_admin_password: str = "admin123"
    day_state_reset_disabled: bool = False

    # ---- Geocoding (CR-019) ----
    geocoding_backend: str = "nominatim"  # "nominatim" | "centroide_comuna"

    # ---- Alerts (CR-022) ----
    alerts_grace_min: int = 15  # eta_breach grace: visita is breach when sim_now > eta + grace
    alerts_preview_min: int = 15  # eta_preview window: visita due within this many minutes
    alerts_vip_deadline_min: int = 30  # vip_deadline: VIP visitas due within this many minutes
    alerts_scheduler_enabled: bool = True  # off-switch for tests / one-shot deploys

    # ---- Sim auto-progression (CR-030) ----
    # As sim_clock advances, visitas whose ETA expired by `grace_min` are
    # auto-completed (entregado | no_entregado, weighted by VIP). Pure sim
    # behavior — no human user is attributed in the audit log.
    sim_progression_enabled: bool = True
    sim_progression_interval_s: int = 30
    sim_progression_grace_min: int = 2
    sim_progression_max_per_tick: int = 100
    sim_progression_success_rate_default: float = 0.92
    sim_progression_success_rate_vip: float = 0.99

    # ---- Observability ----
    app_insights_connection_string: str = ""
    log_level: str = "INFO"

    # ---- Validators (fail-fast) ----
    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_required(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if raw in _PLACEHOLDER_TOKENS or len(raw) < 32:
            raise ValueError(
                "JWT_SECRET is missing or a placeholder. Set it to a random "
                "string of ≥32 chars: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return v

    @field_validator("db_password")
    @classmethod
    def _db_password_required(cls, v: SecretStr, info: ValidationInfo) -> SecretStr:
        raw = v.get_secret_value()
        if raw in _PLACEHOLDER_TOKENS:
            raise ValueError("DB_PASSWORD is missing or a placeholder.")
        return v

    @field_validator("db_server", "db_name", "db_user")
    @classmethod
    def _db_strings_required(cls, v: str, info: ValidationInfo) -> str:
        if not v or v in _PLACEHOLDER_TOKENS:
            raise ValueError(f"{info.field_name.upper()} is required.")
        return v

    @field_validator("db_schema")
    @classmethod
    def _db_schema_safe(cls, v: str) -> str:
        """`db_schema` is interpolated into raw SQL (recipients view query), so
        restrict it to a SQL identifier (or empty for SQLite tests). Closes the
        injection-shaped path (bandit B608)."""
        if v == "" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v):
            return v
        raise ValueError("DB_SCHEMA must be empty or a valid SQL identifier")

    # ---- Derived ----
    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def db_url_async(self) -> str:
        """SQLAlchemy async URL for aioodbc.

        Azure SQL `DB_SERVER` typically comes as `tcp:host.windows.net,1433`
        (ODBC connection-string fragment). SQLAlchemy URLs need `host:port`,
        so we normalize here.
        """
        from urllib.parse import quote_plus

        server = self.db_server
        if server.lower().startswith("tcp:"):
            server = server[4:]
        server = server.replace(",", ":")  # `host,1433` → `host:1433`

        pwd_enc = quote_plus(self.db_password.get_secret_value())
        driver_enc = quote_plus(self.db_driver)
        return (
            f"mssql+aioodbc://{self.db_user}:{pwd_enc}@{server}/{self.db_name}"
            f"?driver={driver_enc}&Encrypt=yes&TrustServerCertificate=no"
            f"&MARS_Connection=Yes"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor. Cached so tests can override via dependency injection."""
    return Settings()


# Convenience module-level singleton (read at import time will fail-fast).
settings = get_settings()
