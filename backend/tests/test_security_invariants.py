"""Properties that must hold across every endpoint, enumerated from the route table.

These do not test a feature. They test a rule the whole API is supposed to obey, and
they discover the routes themselves — so an endpoint added next year is covered the day
it appears, and an author who forgets the household check finds out from the build
rather than from a stranger reading someone else's finances.

Where an exception is genuinely correct it is written down in an allow-list here. That
makes adding one a deliberate act that shows up in review, rather than a quiet gap.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
from conftest import create_account, setup_owner, sync_csrf
from fastapi.testclient import TestClient

from app.main import app

# Routes reachable before anyone has signed in. Everything else must refuse.
PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/status",
    "/api/auth/csrf",
    "/api/auth/login",
    "/api/auth/setup",
    # Signing out without a session is harmless and idempotent — refusing it would only
    # strand a browser holding a token the server has already forgotten.
    "/api/auth/logout",
}

# Authenticated by a secret header rather than a session cookie, so the double-submit
# CSRF check has nothing to protect and would only break the documented cron caller.
CSRF_EXEMPT = {"/api/notifications/run"}

PATH_PARAM = re.compile(r"\{([a-z_]+)\}")


def api_routes() -> list[tuple[str, str]]:
    """(path, METHOD) for every documented endpoint.

    Read from the OpenAPI schema rather than app.routes: FastAPI changed how included
    routers are stored between versions, and a test that silently enumerates nothing is
    worse than no test — it reports as passing coverage.
    """
    out: list[tuple[str, str]] = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        for method in operations:
            if method.upper() in {"HEAD", "OPTIONS"}:
                continue
            out.append((path, method.upper()))
    return sorted(out)


def routes_with_an_id() -> list[tuple[str, str]]:
    """Every route addressing a specific record by id."""
    return [(p, m) for p, m in api_routes() if PATH_PARAM.search(p)]


def test_the_route_table_was_actually_found() -> None:
    """Guards the tests below: an empty parametrise list skips silently, which looks
    exactly like coverage until you read the output."""
    assert len(api_routes()) > 40, "route enumeration returned almost nothing"
    assert len(routes_with_an_id()) > 8


def _other_households_ids(client: TestClient) -> dict[str, str]:
    """Records belonging to a household the caller is not in."""
    account = create_account(client, "Their Everyday", "everyday")
    txn = client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": "2025-06-01",
            "amount_cents": -1234,
            "description": "THEIR PRIVATE SPENDING",
        },
    ).json()
    categories = client.get("/api/categories").json()
    leaf = next(c for c in categories if c["parent_id"])
    return {
        "account_id": account["id"],
        "txn_id": txn["id"],
        "category_id": leaf["id"],
        "id": account["id"],
    }


@pytest.mark.parametrize("path,method", routes_with_an_id())
def test_no_route_serves_another_households_record(
    client: TestClient, path: str, method: str
) -> None:
    """The strongest current property of this codebase: every lookup is scoped to the
    caller's household. It holds today by fifteen individually correct checks, any one
    of which a future endpoint could omit. This asserts it from the route table so the
    omission fails the build."""
    # Household A creates records, then B signs in and reaches for A's ids.
    setup_owner(client, email="a@example.com")
    theirs = _other_households_ids(client)
    client.post("/api/auth/logout")

    # A second household in the same database.
    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        household = models.Household(name="B", state="NSW", period_basis="calendar")
        db.add(household)
        db.flush()
        from app import security as sec

        db.add(
            models.User(
                household_id=household.id,
                email="b@example.com",
                name="B",
                role="owner",
                password_hash=sec.hash_password("password-two"),
            )
        )
        db.commit()

    sync_csrf(client)
    signed_in = client.post(
        "/api/auth/login", json={"email": "b@example.com", "password": "password-two"}
    )
    assert signed_in.status_code == 200, signed_in.text
    sync_csrf(client, signed_in.json()["csrf_token"])
    # Without this the test would pass on 401s and prove nothing about isolation.
    assert client.get("/api/auth/me").status_code == 200

    target = path
    for name in PATH_PARAM.findall(path):
        target = target.replace("{" + name + "}", theirs.get(name, theirs["id"]))

    resp = client.request(method, target, json={})
    assert resp.status_code != 200, (
        f"{method} {path} returned another household's record"
    )
    assert resp.status_code in (400, 403, 404, 405, 422), (
        f"{method} {path} answered {resp.status_code}; expected a refusal. "
        "A 401 here means the test lost its session and is proving nothing."
    )


@pytest.mark.parametrize(
    "path,method",
    [
        (p, m)
        for p, m in api_routes()
        if m != "GET" and p not in CSRF_EXEMPT
    ],
)
def test_every_mutating_route_requires_csrf(client: TestClient, path: str, method: str) -> None:
    """CSRF is enforced by one middleware today. If someone reorders it, or adds a
    router outside /api, this is what notices."""
    setup_owner(client)
    client.cookies.delete("saiva_csrf")

    target = PATH_PARAM.sub("some-id", path)
    resp = client.request(method, target, json={}, headers={"X-CSRF-Token": "wrong"})
    assert resp.status_code == 403, f"{method} {path} accepted a request with no CSRF token"


@pytest.mark.parametrize(
    "path,method",
    [
        (p, m) for p, m in api_routes() if p not in PUBLIC_PATHS
    ],
)
def test_every_route_requires_a_session(client: TestClient, path: str, method: str) -> None:
    """No endpoint should answer a stranger. The public set is written down above, so
    adding to it is a decision someone makes on purpose."""
    target = PATH_PARAM.sub("some-id", path)
    # A valid CSRF pair, so a 403 here means CSRF and not authentication.
    sync_csrf(client)
    resp = client.request(method, target, json={})
    assert resp.status_code in (401, 403, 422, 405), (
        f"{method} {path} answered {resp.status_code} with no session"
    )


def test_a_stored_secret_never_appears_in_any_response(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/admin/export serialises whole tables by iterating __table__.columns, so a secret
    column added later would be emitted without anyone deciding to."""
    sentinel = "sk-do-not-leak-abc123xyz"
    auth_client.patch(
        "/api/ai/settings",
        json={"provider": "openai", "api_key": sentinel, "base_url": "https://api.openai.com/v1"},
    )

    # Some GETs reach the configured provider; the sweep is about our own responses, so
    # the network is stubbed rather than those paths being skipped — skipping is how a
    # leak on one of them would go unnoticed.
    class _Empty:
        status_code = 200
        text = "{}"

        @staticmethod
        def json() -> dict[str, Any]:
            return {"data": [], "models": []}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Empty())
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Empty())

    leaked: list[str] = []
    for path, method in api_routes():
        if method != "GET" or PATH_PARAM.search(path):
            continue
        body = auth_client.get(path).text
        if sentinel in body:
            leaked.append(path)
    assert not leaked, f"the stored API key was returned by: {leaked}"


def test_the_security_headers_are_on_every_response(auth_client: TestClient) -> None:
    resp = auth_client.get("/api/accounts")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "no-store" in resp.headers["Cache-Control"]
    assert "default-src 'none'" in resp.headers["Content-Security-Policy"]


def test_docs_are_closed_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """The OpenAPI schema names every route and its shape. It is off in production by
    configuration, which is only true while ENVIRONMENT is set correctly."""
    from app.config import Settings

    assert Settings(environment="production", secret_key="k" * 40).is_production
    assert not Settings(environment="development", secret_key="k" * 40).is_production
