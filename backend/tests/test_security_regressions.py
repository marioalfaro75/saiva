"""Regressions for security defects found in review.

Each test here corresponds to a specific finding. They are written to fail against the
code as it was, not merely to describe the fix — a security test that passes either way
is worse than none, because it reads as coverage.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from conftest import setup_owner
from fastapi.testclient import TestClient

from app import config, security

# ---------------------------------------------------------------- secret quality


def _settings(**over: object) -> config.Settings:
    return config.Settings(
        secret_key=str(over.get("secret_key", "x" * 40)),
        environment=str(over.get("environment", "production")),
    )


@pytest.mark.parametrize(
    "key",
    [
        "dev-insecure-secret-change-me",   # the application default
        "change-me-to-a-long-random-string",  # what .env.example used to ship
        "changeme",
        "short",
    ],
)
def test_production_refuses_a_guessable_secret_key(key: str) -> None:
    """Compose checks SECRET_KEY is set; being set is not the same as being secret.

    A placeholder copied out of .env.example satisfies `${SECRET_KEY:?}` perfectly —
    and it signs every session and encrypts every stored provider key.
    """
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config.check_production_secrets(_settings(secret_key=key))


def test_a_real_secret_is_accepted() -> None:
    config.check_production_secrets(_settings(secret_key="k" * 48))


def test_development_is_left_alone() -> None:
    """A default key is the point of a dev default; only production is policed."""
    config.check_production_secrets(
        _settings(secret_key="dev-insecure-secret-change-me", environment="development")
    )


# ---------------------------------------------------------------- login timing


def test_an_unknown_address_costs_the_same_as_a_known_one(client: TestClient) -> None:
    """Argon2 is deliberately slow, so skipping it for unknown addresses is a timing
    oracle for which emails have accounts. Asserted by counting the work rather than
    by timing it, which would flake on a shared runner."""
    setup_owner(client)

    with patch.object(
        security, "verify_password", wraps=security.verify_password
    ) as verify:
        client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x" * 12})
        unknown_calls = verify.call_count

    with patch.object(
        security, "verify_password", wraps=security.verify_password
    ) as verify:
        client.post("/api/auth/login", json={"email": "owner@example.com", "password": "wrong-one"})
        known_calls = verify.call_count

    assert unknown_calls == known_calls == 1


def test_both_failures_say_the_same_thing(client: TestClient) -> None:
    setup_owner(client)
    unknown = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "x" * 12}
    )
    wrong = client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": "x" * 12}
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


# ---------------------------------------------------------------- notifications token


def test_the_notifications_token_is_compared_in_constant_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain != returns on the first differing byte, so the rejection time reveals how
    much of the prefix was right. Asserted structurally: a near-miss sharing a long
    prefix must be rejected exactly like a value sharing nothing."""
    import hmac

    from app.api import notifications

    calls: list[tuple[str, str]] = []
    real = hmac.compare_digest

    def spy(a: object, b: object) -> bool:
        calls.append((str(a), str(b)))
        return real(a, b)  # type: ignore[arg-type]

    monkeypatch.setattr(
        notifications, "hmac", type("h", (), {"compare_digest": staticmethod(spy)})
    )
    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: type("s", (), {"notifications_token": "s3cret-token"})(),
    )

    for attempt in ("s3cret-toke_", "zzzzzzzzzzzz"):
        resp = client.post("/api/notifications/run", headers={"X-Notify-Token": attempt})
        assert resp.status_code == 401

    assert len(calls) == 2, "the token must go through compare_digest, not =="


def test_no_token_configured_means_no_access(client: TestClient) -> None:
    """An unset token must not become an open door."""
    assert client.post("/api/notifications/run").status_code == 401


def test_the_documented_cron_call_reaches_the_token_check(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The README tells operators to run

        curl -X POST -H "X-Notify-Token: $TOKEN" https://host/api/notifications/run

    from cron. CSRF middleware applied to every non-safe /api path, so that request was
    rejected 403 before the token was looked at — the alerts cron could never have run.
    An endpoint authenticated by a secret header has no CSRF exposure to begin with.
    """
    from app.api import notifications

    monkeypatch.setattr(
        notifications,
        "get_settings",
        lambda: type("s", (), {"notifications_token": "cron-token"})(),
    )
    monkeypatch.setattr(
        notifications.svc,
        "run_all",
        lambda db: {"households": 0, "created": 0, "emailed": 0, "digests": 0},
    )

    # No CSRF cookie or header, exactly as cron sends it.
    ok = client.post("/api/notifications/run", headers={"X-Notify-Token": "cron-token"})
    assert ok.status_code == 200, ok.text

    bad = client.post("/api/notifications/run", headers={"X-Notify-Token": "wrong"})
    assert bad.status_code == 401, "exempt from CSRF, still authenticated"
