"""Application configuration, sourced from environment variables (or a local .env)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    app_name: str = "Saiva"
    environment: str = "development"

    # SQLAlchemy URL. Postgres in production; SQLite is used for fast tests.
    database_url: str = "postgresql+psycopg://saiva:saiva@localhost:5432/saiva"

    # Secret used to sign session cookies. MUST be overridden in production.
    secret_key: str = "dev-insecure-secret-change-me"
    session_ttl_minutes: int = 60 * 24 * 14  # 14 days

    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    # Additional allowed CORS origins (comma-separated). Empty in same-origin deployments.
    cors_origins: str = ""

    rate_limit_login_per_minute: int = 10

    default_currency: str = "AUD"
    default_locale: str = "en-AU"

    # Software updates
    saiva_version: str = "dev"  # baked into the image at build time (SAIVA_VERSION)
    update_check_enabled: bool = True
    update_repo: str = "marioalfaro75/saiva"
    watchtower_url: str = ""  # e.g. http://watchtower:8080 (empty = in-app apply disabled)
    watchtower_token: str = ""

    # Email / SMTP for notifications & digests. An empty host disables all email.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: bool = True  # use STARTTLS (typical on port 587)
    smtp_ssl: bool = False  # implicit TLS (typical on port 465)
    # Shared token the cron caller passes to POST /api/notifications/run.
    notifications_token: str = ""

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
