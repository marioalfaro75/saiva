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

    # Per-caller, per-minute ceilings. 0 disables one. Credentials cover login,
    # first-run setup and password change; the rest are the endpoints that cost the
    # most per request, which is what makes them worth sending in a loop.
    rate_limit_login_per_minute: int = 10
    rate_limit_import_per_minute: int = 20
    rate_limit_ai_per_minute: int = 20
    rate_limit_report_per_minute: int = 30

    # Reverse proxies whose X-Forwarded-For may be believed: comma-separated IPs,
    # CIDRs, or hostnames (Compose service names resolve here). Empty means trust
    # nothing and use the peer address, which is correct when nothing fronts the API.
    trusted_proxies: str = ""

    default_currency: str = "AUD"
    default_locale: str = "en-AU"

    # Software updates
    #
    # Two identities, because one string could not carry both jobs and the attempt to
    # make it do so put the update badge permanently out of action on three channels.
    #
    # `saiva_version` answers "which release is this?" and is compared against GitHub
    # releases, so it must be semver-shaped or the comparison is meaningless.
    # `parse_version` returns None for anything else, `is_newer` returns False on None,
    # and the panel then renders "You're on the latest version" forever. CI used to bake
    # the literal string `edge` into both the :edge AND the :sha-<short> images, which is
    # how the README's own documented rollback target ended up unable to see an update.
    #
    # `saiva_build` answers "which exact build is this?" — the immutable tag
    # (`sha-1a2b3c4`), or empty on a local build. It is never compared; it is what the
    # panel shows when there is no release to compare against, and what the deploy
    # records so a rollback can name what it went back to.
    saiva_version: str = "dev"  # baked at build time (SAIVA_VERSION); semver or "dev"
    saiva_build: str = ""  # baked at build time (SAIVA_BUILD); immutable tag, e.g. sha-1a2b3c4
    update_check_enabled: bool = True
    update_repo: str = "marioalfaro75/saiva"
    # SAIVA_UPDATE_DIR is deliberately NOT a setting here. It is read straight from the
    # environment in app/services/updates.py, at call time, because it describes the
    # container's filesystem rather than the application's behaviour: whether the
    # directory exists and is writable is a question only the filesystem can answer, and
    # a cached settings object would hold a stale yes. Empty or missing means in-app
    # updates are not set up on this box, which is a normal state the panel reports
    # plainly.

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


# Values that have been published — in this repository, in the README, in a tutorial.
# Being set is not the same as being secret, and the compose guard only checks the former.
PUBLISHED_SECRETS = {
    "dev-insecure-secret-change-me",
    "change-me-to-a-long-random-string",
    "changeme",
    "secret",
}
MIN_SECRET_KEY_LENGTH = 32


def check_production_secrets(settings: "Settings") -> None:
    """Refuse to run in production on a key anyone could guess.

    SECRET_KEY signs every session token and derives the key that encrypts stored AI
    provider credentials, so a known value means forgeable sessions and readable
    secrets. Compose already refuses to start without one, but a placeholder copied
    from .env.example satisfies that check — the variable is set, it is simply not
    secret. This is the check that cares which.
    """
    if not settings.is_production:
        return
    key = settings.secret_key
    if key.strip().lower() in PUBLISHED_SECRETS or len(key) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            "SECRET_KEY is a placeholder or too short. It signs your sessions and "
            "encrypts your stored API keys, so it must be unique and unguessable.\n"
            "Generate one with:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    check_production_secrets(settings)
    return settings
