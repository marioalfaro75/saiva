"""/api/health is the only signal every gate in the deployment resolves to.

The image's HEALTHCHECK probes it, scripts/deploy.sh waits on it, and `compose up --wait`
would too. It used to be `return {"status": "ok"}` — a literal, meaning "uvicorn is
listening" — so every one of those gates was measuring the same unconditional 200. During
the outage that took the site off the LAN, the deploy script printed a tick and then
printed the URL nobody could reach.

The tests here fix two properties in place:

  * it reaches the database, so "listening but cannot query" is not reported as healthy;
  * it reports the schema state as DATA and stays 200 for `ahead`.

The second one is subtle enough to be worth spelling out, because the obvious design
breaks the rollback story. If health asserted `current == head` and returned 503
otherwise, then an image rolled back onto a newer schema — which app/dbinit.py now lets
serve, deliberately — would serve correctly while reporting itself unhealthy. The deploy
gate would time out, declare the rollback a failure and roll back again, with `restart:
unless-stopped` looping underneath. A boolean cannot carry a four-valued answer.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app.schema_state import AHEAD, BEHIND, CURRENT, SchemaState


def test_health_reports_what_it_can_see(client: TestClient) -> None:
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["image_head"], "health must name the schema revision this image carries"
    assert "version" in body and "schema" in body


def test_health_is_200_and_honest_when_the_database_is_ahead(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression guard for the gate-thrash failure described in this module's docstring."""
    monkeypatch.setattr(
        app_main, "read_schema_state", lambda engine, cfg=None, conn=None: SchemaState(AHEAD, "0099", "0012")
    )

    response = client.get("/api/health")

    assert response.status_code == 200, (
        "a rolled-back image serves; reporting it unhealthy makes the deploy gate roll "
        "back in a loop"
    )
    body = response.json()
    assert body["schema"] == AHEAD
    assert body["db_revision"] == "0099"
    assert body["image_head"] == "0012"
    assert body["status"] == "ok"


@pytest.mark.parametrize("state", [CURRENT, BEHIND, AHEAD])
def test_every_schema_state_is_servable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    """No schema reading may make this endpoint fail. Only an unreachable database may."""
    monkeypatch.setattr(
        app_main, "read_schema_state", lambda engine, cfg=None, conn=None: SchemaState(state, "0001", "0012")
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["schema"] == state


class _Unreachable:
    """Stands in for the engine while a test needs the database to be unreachable.

    Patched over the NAME in app.main, not over the shared engine object: conftest's
    schema fixture tears down through `app.db.engine`, which it imported by value, so
    breaking the real engine would break teardown for every test in the session.
    """

    def __init__(self, message: str = "connection refused") -> None:
        self.message = message

    def connect(self, *_args: object, **_kwargs: object) -> None:
        raise OSError(self.message)


def test_health_is_503_when_the_database_cannot_be_reached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one failure this endpoint exists to surface, and the one a restart might fix."""
    monkeypatch.setattr(app_main, "db_engine", _Unreachable())

    response = client.get("/api/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"


def test_an_unreadable_migration_history_is_not_reported_as_a_dead_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two failures are different and the fix for each is different.

    `database: error` + 503 is the verdict reserved for "this process cannot reach the
    database", which a restart or a db container coming back may fix. A database that
    answers while the image cannot read its own migration history is a different problem
    and must not be reported as the first one -- it sends whoever is debugging to the
    wrong half of the system, and fails a deploy gate over something no restart fixes.
    """

    def unreadable(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Can't locate revision identified by 'nonsense'")

    monkeypatch.setattr(app_main, "read_schema_state", unreadable)

    response = client.get("/api/health")

    assert response.status_code == 200, "the database is fine; only the history is not"
    body = response.json()
    assert body["database"] == "ok"
    assert body["schema"] == "unknown"
    assert body["detail"] == "RuntimeError"


def test_the_schema_state_really_is_read_on_every_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guards the tests above, which pass whatever the endpoint reports if it never asks.

    Every other test here patches read_schema_state, so none of them notices if the
    endpoint stops calling it. This one counts the calls.
    """
    calls: list[int] = []
    real = app_main.read_schema_state

    def counting(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(app_main, "read_schema_state", counting)

    client.get("/api/health")
    client.get("/api/health")

    assert len(calls) == 2, "the endpoint is not reading the schema state per request"


def test_health_holds_one_connection_per_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two at once on every 30-second probe is how a health check exhausts a small pool."""
    open_now = 0
    high_water = 0
    real_connect = app_main.db_engine.connect

    def counting_connect(*args: object, **kwargs: object) -> object:
        nonlocal open_now, high_water
        conn = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        open_now += 1
        high_water = max(high_water, open_now)
        original_close = conn.close

        def close() -> None:
            nonlocal open_now
            open_now -= 1
            original_close()

        conn.close = close  # type: ignore[method-assign]
        return conn

    monkeypatch.setattr(app_main.db_engine, "connect", counting_connect)

    client.get("/api/health")

    assert high_water == 1, f"health held {high_water} connections at once"


def test_health_leaks_nothing_about_the_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is reachable without authentication, so the detail is a type name, not a DSN.

    A SQLAlchemy OperationalError's string carries the connection URL, host and user.
    Health is in main.py's unauthenticated set on purpose — the HEALTHCHECK runs with no
    credentials — so the body must stay useless to anyone who is not the owner.
    """

    monkeypatch.setattr(
        app_main, "db_engine", _Unreachable("could not connect to 10.0.0.5 as user hunter2")
    )

    body = client.get("/api/health").json()

    assert body.get("detail") == "OSError"
    assert "hunter2" not in str(body) and "10.0.0.5" not in str(body)
