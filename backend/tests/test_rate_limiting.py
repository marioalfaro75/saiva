"""Rate limiting: who gets counted, what is covered, and what it costs to remember.

The limiter used to protect one endpoint, count every visitor as the same person,
and remember them forever. Each of those is tested here separately, because each
fails in a way the others hide: a limiter that works perfectly on one shared bucket
still passes a naive "does it return 429" test.
"""

from __future__ import annotations

import ipaddress

import pytest
from conftest import DEFAULT_PASSWORD, setup_owner, sync_csrf
from fastapi.testclient import TestClient

from app import clientip, ratelimit


@pytest.fixture(autouse=True)
def _clean_limiter() -> None:
    ratelimit.reset()


def _limit_to(monkeypatch: pytest.MonkeyPatch, name: str, value: int) -> None:
    monkeypatch.setattr(ratelimit.settings, name, value)


def test_login_still_stops_after_the_configured_number_of_attempts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _limit_to(monkeypatch, "rate_limit_login_per_minute", 3)
    sync_csrf(client)
    codes = [
        client.post(
            "/api/auth/login", json={"email": "nobody@example.com", "password": "x"}
        ).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [401, 401, 401]
    assert codes[3:] == [429, 429]


def test_two_callers_do_not_share_one_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behind a proxy every request has the same peer address.

    Keyed on that, one visitor's failed logins locked out the whole household, and
    an attacker got the entire budget to themselves. The header only counts because
    the peer is a proxy we were told to trust.
    """
    _limit_to(monkeypatch, "rate_limit_login_per_minute", 2)
    monkeypatch.setattr(clientip, "_networks", [ipaddress.ip_network("10.0.0.0/8")])
    monkeypatch.setattr(clientip, "_hostnames", [])

    from app.main import app

    # The peer is the proxy, which is what makes its X-Forwarded-For readable.
    with TestClient(app, client=("10.0.0.2", 50000)) as client:
        sync_csrf(client)
        attacker = {"X-Forwarded-For": "203.0.113.9"}
        victim = {"X-Forwarded-For": "198.51.100.4"}
        body = {"email": "nobody@example.com", "password": "x"}

        for _ in range(3):
            client.post("/api/auth/login", json=body, headers=attacker)
        assert client.post("/api/auth/login", json=body, headers=attacker).status_code == 429
        # The victim, having done nothing, is unaffected.
        assert client.post("/api/auth/login", json=body, headers=victim).status_code == 401


def test_a_forwarded_header_from_an_untrusted_peer_is_ignored(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the limit is no limit: every request claims a fresh address."""
    _limit_to(monkeypatch, "rate_limit_login_per_minute", 2)
    monkeypatch.setattr(clientip, "_networks", [])
    monkeypatch.setattr(clientip, "_hostnames", [])
    sync_csrf(client)
    body = {"email": "nobody@example.com", "password": "x"}

    codes = [
        client.post(
            "/api/auth/login", json=body, headers={"X-Forwarded-For": f"203.0.113.{n}"}
        ).status_code
        for n in range(5)
    ]
    assert 429 in codes, "spoofed forwarded addresses bought an unlimited number of attempts"


def test_the_rightmost_forwarded_entry_wins() -> None:
    """A client can put anything in the header; only what our proxy appended is real."""
    from starlette.datastructures import Headers
    from starlette.requests import Request

    def request_from(peer: str, forwarded: str | None) -> Request:
        raw = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
        return Request({"type": "http", "client": (peer, 1234), "headers": Headers(raw=raw).raw})

    original = clientip._networks[:]
    try:
        clientip._networks[:] = [ipaddress.ip_network("10.0.0.0/8")]
        # The caller forged the first entry; Caddy appended the address it saw.
        forged = request_from("10.0.0.2", "1.2.3.4, 198.51.100.7")
        assert clientip.client_ip(forged) == "198.51.100.7"
        assert clientip.client_ip(request_from("10.0.0.2", None)) == "10.0.0.2"
        # An untrusted peer's header is not read at all.
        assert clientip.client_ip(request_from("203.0.113.1", "1.2.3.4")) == "203.0.113.1"
    finally:
        clientip._networks[:] = original


def test_the_bucket_store_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded dict keyed on caller address is a memory leak anyone can pull."""
    monkeypatch.setattr(ratelimit, "MAX_TRACKED_KEYS", 50)
    for n in range(500):
        ratelimit._record(f"credentials:198.51.100.{n}", 10)
    assert len(ratelimit._hits) <= 50


def test_password_change_is_rate_limited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It takes the current password, so it is guessable exactly as login is."""
    setup_owner(client)
    _limit_to(monkeypatch, "rate_limit_login_per_minute", 3)
    ratelimit.reset()
    body = {"current_password": "wrong-password", "new_password": "another-password-1!"}
    codes = [client.post("/api/auth/password", json=body).status_code for _ in range(5)]
    assert 429 in codes, "an authenticated caller could brute-force the current password"


def test_the_importer_is_rate_limited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Parsing a statement costs megabytes of work; login costs one hash."""
    setup_owner(client)
    _limit_to(monkeypatch, "rate_limit_import_per_minute", 2)
    ratelimit.reset()
    csv = b"Date,Description,Amount\n2026-01-01,Coffee,-4.50\n"
    codes = [
        client.post("/api/imports/sniff", files={"file": ("s.csv", csv, "text/csv")}).status_code
        for _ in range(4)
    ]
    assert 429 in codes


def test_the_advisor_is_rate_limited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each call leaves the machine and, on a paid provider, spends money."""
    setup_owner(client)
    _limit_to(monkeypatch, "rate_limit_ai_per_minute", 2)
    ratelimit.reset()
    codes = [
        client.post("/api/ai/chat", json={"message": "hello"}).status_code for _ in range(4)
    ]
    assert 429 in codes


def test_the_limits_do_not_share_a_bucket(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhausting one group must not lock a household out of signing in."""
    setup_owner(client)
    _limit_to(monkeypatch, "rate_limit_ai_per_minute", 1)
    _limit_to(monkeypatch, "rate_limit_login_per_minute", 5)
    ratelimit.reset()
    for _ in range(4):
        client.post("/api/ai/chat", json={"message": "hello"})
    assert client.post("/api/ai/chat", json={"message": "hello"}).status_code == 429
    signin = client.post(
        "/api/auth/login", json={"email": "owner@example.com", "password": DEFAULT_PASSWORD}
    )
    assert signin.status_code == 200
