"""Test configuration: an isolated SQLite database and authentication helpers.

Environment is set *before* importing the app so the engine binds to SQLite.
"""

from __future__ import annotations

import os
import tempfile

os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{tempfile.mkdtemp()}/test.db"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["COOKIE_SECURE"] = "false"
os.environ["CORS_ORIGINS"] = ""
# Effectively off by default: a test that hammers an endpoint is testing that
# endpoint, and a stray 429 would read as the feature being broken. The tests
# that are about the limiter set their own ceilings.
os.environ["RATE_LIMIT_LOGIN_PER_MINUTE"] = "1000"
os.environ["RATE_LIMIT_IMPORT_PER_MINUTE"] = "10000"
os.environ["RATE_LIMIT_AI_PER_MINUTE"] = "10000"
os.environ["RATE_LIMIT_REPORT_PER_MINUTE"] = "10000"

import datetime as dt  # noqa: E402
from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (register models on metadata)
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

DEFAULT_PASSWORD = "password123!"


@pytest.fixture(autouse=True)
def _fresh_rate_limiter() -> None:
    """The limiter is process-lifetime state; without this it leaks across tests."""
    from app import ratelimit

    ratelimit.reset()


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def today() -> dt.date:
    """Today's date, captured once for the test that asks for it.

    Five test modules used to hold `TODAY = dt.date.today()` at module level, evaluated
    when pytest imported them during collection. The services compute their own date per
    request — `today = today or dt.date.today()` in a dozen of them — so a suite run that
    crossed UTC midnight compared expectations built for one day against behaviour on the
    next. It is not hypothetical: CI run 41364727610 started at 23:59:41Z and failed
    test_forecast_shape_and_income with `'2026-11-02' == '2026-11-01'`, 620 other tests
    passing. The same commit was green on re-run, and the red made an unrelated Dependabot
    PR look broken.

    Why this and not a frozen clock. The services do take an injectable `today`, but these
    tests drive the app over HTTP and the API layer never passes it, so injection is out of
    reach. freezegun would close the window exactly, and it patches `time.monotonic` — which
    app/clientip.py uses for rate limiting and two tests in test_security_regressions.py
    assert elapsed time against. Buying determinism in the date by freezing the clock out
    from under the rate limiter is a bad trade for a test-hygiene fix.

    So the window is narrowed rather than eliminated, and it is worth stating the size
    instead of calling it negligible. Measured on test_forecast_shape_and_income: the call
    phase is 0.32s and this fixture resolves during setup, so the gap between reading the
    date and asserting on it is around 0.35s. Before, that gap ran from collection to
    whenever the test happened to execute — anywhere inside a ~250s suite. For a run
    starting at a uniformly random time that is roughly 1 failure in 250,000 runs, against
    about 1 in 350 before.

    It is a 700x reduction, not a proof: a test that seeds and asserts across midnight
    inside the same third of a second still fails. Closing it completely means either
    freezegun, rejected above, or routing twenty production call sites through an
    injectable clock — a refactor of app code to suit the tests, on a service holding real
    financial records. test_no_test_module_captures_the_date_at_import_time keeps the old,
    suite-wide version from coming back, which is where the actual risk was.
    """
    return dt.date.today()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def sync_csrf(client: TestClient, token: str | None = None) -> None:
    if token is None:
        token = client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers["X-CSRF-Token"] = token


def setup_owner(
    client: TestClient,
    email: str = "owner@example.com",
    password: str = DEFAULT_PASSWORD,
) -> dict:
    sync_csrf(client)
    resp = client.post(
        "/api/auth/setup",
        json={
            "household_name": "Test Household",
            "name": "Owner",
            "email": email,
            "password": password,
            "state": "NSW",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    sync_csrf(client, body["csrf_token"])
    return body


@pytest.fixture
def auth_client(client: TestClient) -> TestClient:
    setup_owner(client)
    return client


def create_account(client: TestClient, name: str = "Everyday", type_: str = "everyday") -> dict:
    resp = client.post("/api/accounts", json={"name": name, "type": type_})
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture(autouse=True)
def _no_outbound_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test reaches the internet.

    An advisor test once stubbed the wrong function and the suite quietly called a
    real provider API with a fake key. Tests that want a canned provider response
    still patch httpx themselves inside the test body, which takes precedence.
    """
    import httpx

    def blocked(*args: object, **kwargs: object) -> None:
        target = args[0] if args else kwargs.get("url", "?")
        raise AssertionError(
            f"Test tried to make a real HTTP request to {target!r}. Stub it instead."
        )

    for name in ("get", "post", "put", "patch", "delete", "request"):
        monkeypatch.setattr(httpx, name, blocked)
