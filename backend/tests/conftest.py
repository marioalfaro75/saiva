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
os.environ["RATE_LIMIT_LOGIN_PER_MINUTE"] = "1000"

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402,F401  (register models on metadata)
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

DEFAULT_PASSWORD = "password123!"


@pytest.fixture(autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


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
