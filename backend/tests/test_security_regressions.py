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


# ---------------------------------------------------------------- SSRF


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "http://localhost/v1",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://watchtower:8080",                    # the neighbouring container
        "https://10.0.0.5/v1",
        "https://192.168.1.10",
        "file:///etc/passwd",
        "gopher://internal",
    ],
)
def test_a_provider_url_pointing_inward_is_refused_on_save(
    auth_client: TestClient, url: str
) -> None:
    """base_url was written straight through and the advisor POSTed to it, so any adult
    could aim the server at whatever the container can reach — including the Watchtower
    API on the same Compose network, which holds the Docker socket."""
    resp = auth_client.patch("/api/ai/settings", json={"provider": "openai", "base_url": url})
    assert resp.status_code == 400, f"{url} should be refused, got {resp.status_code}"


def test_a_real_provider_endpoint_is_accepted(auth_client: TestClient) -> None:
    resp = auth_client.patch(
        "/api/ai/settings", json={"provider": "openai", "base_url": "https://api.openai.com/v1"}
    )
    assert resp.status_code == 200, resp.text


def test_the_check_runs_again_before_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A value stored before this rule existed must not still be requested. Asserted at
    the service boundary, since the API layer would refuse to store it now."""
    from app.services import advisor

    stored = type(
        "Ai", (), {"base_url": "https://169.254.169.254", "provider": "openai",
                   "privacy_mode": "full"}
    )()
    with pytest.raises(advisor.ProviderError, match="inside your own network"):
        advisor._check_endpoint(stored)


def test_an_unset_base_url_is_fine() -> None:
    """Unset means the provider's own public endpoint, which is the common case."""
    from app.services import advisor

    advisor._check_endpoint(
        type("Ai", (), {"base_url": None, "provider": "openai", "privacy_mode": "full"})()
    )


# ---------------------------------------------------------------- local-only means local


def _ai(mode: str, base_url: str | None):
    return type("Ai", (), {"privacy_mode": mode, "base_url": base_url, "provider": "openai"})()


def test_local_only_refuses_a_cloud_provider() -> None:
    """The advisor page labels this mode "nothing leaves your network". It used to mean
    only that a different amount of context was assembled — the data still went to
    whichever cloud provider was configured. That made the interface state something
    untrue about a family's financial records."""
    from app.services import advisor

    with pytest.raises(advisor.ProviderError, match="outside your network"):
        advisor._check_endpoint(_ai("local_only", "https://api.openai.com/v1"))


def test_local_only_requires_an_endpoint_to_be_local_to() -> None:
    from app.services import advisor

    with pytest.raises(advisor.ProviderError, match="Base URL"):
        advisor._check_endpoint(_ai("local_only", None))


def test_local_only_allows_a_model_on_your_own_machine() -> None:
    """Ollama and the like run on loopback over plain http, which the SSRF rule refuses
    for every other mode. This is the one place that exception belongs."""
    from app.services import advisor

    advisor._check_endpoint(_ai("local_only", "http://localhost:11434/v1"))
    advisor._check_endpoint(_ai("local_only", "http://192.168.1.50:11434/v1"))


def test_the_other_modes_still_refuse_a_local_endpoint() -> None:
    """The local exception must not leak into modes that talk to the internet, or it
    becomes the SSRF hole again by another name."""
    from app.services import advisor

    for mode in ("full", "aggregates"):
        with pytest.raises(advisor.ProviderError):
            advisor._check_endpoint(_ai(mode, "http://127.0.0.1:8080"))


# ---------------------------------------------------------------- denial of service


def test_a_catastrophic_rule_pattern_returns_instead_of_hanging() -> None:
    """Rule patterns are written by household members and run against every transaction
    on every import. Under the stdlib engine "^(a+)+$" against 31 characters takes about
    55 seconds, with nothing bounding it — so any signed-in member, a read-only viewer
    included, could pin a core for as long as they liked."""
    import time

    from app.services.categorise import _regex_matches

    started = time.monotonic()
    assert _regex_matches(r"^(a+)+$", "a" * 40 + "!") is False
    assert time.monotonic() - started < 1.0


def test_ordinary_rule_patterns_still_work() -> None:
    from app.services.categorise import _regex_matches

    assert _regex_matches(r"wool\w+", "WOOLWORTHS METRO") is True
    assert _regex_matches(r"^COLES", "WOOLWORTHS") is False
    assert _regex_matches(r"[unclosed", "anything") is False


def test_an_unterminated_ofx_tag_does_not_backtrack() -> None:
    """`<TAG>(.*?)</TAG>` backtracks quadratically when a tag is opened and never
    closed. 10 MB of `<STMTTRN>` — which any member can upload to /imports/preview —
    cost hours of CPU. The scanner is linear whatever the input."""
    import time

    from app.services import importers

    payload = b"<STMTTRN>" * 120_000  # ~1 MB, all unterminated
    started = time.monotonic()
    assert importers.parse_ofx(payload) == []
    assert time.monotonic() - started < 2.0


def test_an_oversized_body_is_refused_on_its_declared_length(
    auth_client: TestClient,
) -> None:
    """The cap used to be checked after `await file.read()` had buffered the whole body,
    so a 4 GB upload landed on the disk the database dumps share before anything looked
    at its size."""
    resp = auth_client.post(
        "/api/imports/sniff",
        files={"file": ("big.csv", b"x" * 100, "text/csv")},
        headers={"Content-Length": str(50 * 1024 * 1024)},
    )
    assert resp.status_code == 413


def test_a_file_over_the_cap_is_refused_while_reading(auth_client: TestClient) -> None:
    """Content-Length can be absent or a lie, so the route-level cap still has to hold —
    now enforced chunk by chunk rather than after the fact."""
    resp = auth_client.post(
        "/api/imports/sniff",
        files={"file": ("big.csv", b"x" * (11 * 1024 * 1024), "text/csv")},
    )
    assert resp.status_code == 413
