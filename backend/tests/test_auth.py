from __future__ import annotations

from conftest import DEFAULT_PASSWORD, setup_owner, sync_csrf
from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/api/health").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"


def test_setup_and_me(client: TestClient) -> None:
    body = setup_owner(client)
    assert body["user"]["role"] == "owner"
    assert body["household"]["currency"] == "AUD"
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "owner@example.com"


def test_setup_blocked_when_initialised(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/api/auth/setup",
        json={
            "household_name": "Another",
            "name": "X",
            "email": "x@example.com",
            "password": DEFAULT_PASSWORD,
        },
    )
    assert resp.status_code == 409


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/api/accounts").status_code == 401


def test_login_and_logout(client: TestClient) -> None:
    setup_owner(client)
    assert client.post("/api/auth/logout").status_code == 200
    sync_csrf(client)
    ok = client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": DEFAULT_PASSWORD},
    )
    assert ok.status_code == 200
    sync_csrf(client, ok.json()["csrf_token"])  # login rotates the CSRF token
    bad = client.post(
        "/api/auth/login",
        json={"email": "owner@example.com", "password": "wrong-password-1"},
    )
    assert bad.status_code == 401


def test_csrf_required_for_mutations(auth_client: TestClient) -> None:
    del auth_client.headers["X-CSRF-Token"]
    resp = auth_client.post("/api/accounts", json={"name": "X", "type": "everyday"})
    assert resp.status_code == 403


def test_csrf_endpoint_does_not_rotate_an_existing_token(auth_client: TestClient) -> None:
    """A second tab bootstrapping must not invalidate the tab already open.

    /auth/csrf used to mint a fresh token on every call, so the first tab kept
    sending one the cookie no longer matched and every write it tried failed until
    it was reloaded.
    """
    first = auth_client.get("/api/auth/csrf").json()["csrf_token"]
    second = auth_client.get("/api/auth/csrf").json()["csrf_token"]
    assert first == second
    assert auth_client.cookies.get("saiva_csrf") == first

    # The token handed out earlier still authorises a write.
    auth_client.headers["X-CSRF-Token"] = first
    resp = auth_client.post("/api/accounts", json={"name": "Still works", "type": "everyday"})
    assert resp.status_code == 201, resp.text


def test_csrf_endpoint_mints_a_token_when_there_is_none(client: TestClient) -> None:
    client.cookies.clear()
    token = client.get("/api/auth/csrf").json()["csrf_token"]
    assert token
    assert client.cookies.get("saiva_csrf") == token


def test_taxonomy_seeded(auth_client: TestClient) -> None:
    names = [c["name"] for c in auth_client.get("/api/categories").json()]
    assert "Supermarkets" in names
    assert "Uncategorised" in names
    assert "Internal transfers" in names
    assert len(names) > 40
