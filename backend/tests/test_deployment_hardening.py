"""Invariants about how the app is deployed and who may change what.

The compose and Caddy assertions look unusual in a Python suite, but there is no
other test runner in this repository and these regress in exactly the same silent
way application code does: someone adds a service, mounts the socket for
convenience, and nothing anywhere says no.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml
from conftest import DEFAULT_PASSWORD, sync_csrf
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
COMPOSE_PROD = REPO / "docker-compose.prod.yml"
COMPOSE_DEV = REPO / "docker-compose.yml"
CADDYFILE = REPO / "infra" / "Caddyfile"


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# --- Household configuration is an owner decision -------------------------------


def _second_user(client: TestClient, role: str) -> TestClient:
    """Add a user in the given role and return a client signed in as them."""
    resp = client.post(
        "/api/household/users",
        json={
            "name": "Adult",
            "email": f"{role}@example.com",
            "password": DEFAULT_PASSWORD,
            "role": role,
        },
    )
    assert resp.status_code == 201, resp.text

    from app.main import app

    other = TestClient(app)
    sync_csrf(other)
    login = other.post(
        "/api/auth/login", json={"email": f"{role}@example.com", "password": DEFAULT_PASSWORD}
    )
    assert login.status_code == 200, login.text
    sync_csrf(other, login.json()["csrf_token"])
    return other


def test_a_writer_cannot_reshape_the_household_reporting_periods(
    auth_client: TestClient,
) -> None:
    """`fy_start_month` and `period_basis` decide where every period boundary falls.

    Changing one silently restates every figure the household has been shown, which
    is not a rung below "add a user" — and adding a user was already owner-only.
    """
    adult = _second_user(auth_client, "adult")
    assert adult.get("/api/auth/me").status_code == 200, "the second user never signed in"

    resp = adult.patch("/api/household", json={"fy_start_month": 1, "period_basis": "weekly"})
    assert resp.status_code == 403, (
        "a non-owner restated every historical figure the household reports"
    )


def test_the_owner_can_still_change_it(auth_client: TestClient) -> None:
    """The lock must not have locked the owner out of their own settings."""
    resp = auth_client.patch("/api/household", json={"name": "New Name"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "New Name"


# --- Alerts actually get sent ---------------------------------------------------


def test_an_alert_is_emailed_even_when_a_browser_saw_it_first(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`generate` is idempotent and the feed calls it on every read.

    So if any tab loaded before the cron ran, the notification row already existed,
    nothing was "created", and the alert was never emailed to anyone. Which is most
    of the time — people leave the app open.
    """
    from app import models
    from app.db import SessionLocal
    from app.services import notifications

    household_id = auth_client.get("/api/auth/me").json()["household"]["id"]
    with SessionLocal() as db:
        ns = notifications.settings_for(db, household_id)
        ns.email_enabled = True
        db.add(
            models.Notification(
                household_id=household_id,
                key="budget:groceries:2026-03",
                type="budget",
                severity="alert",
                title="Groceries budget exceeded",
                body="You are over by $40.",
            )
        )
        db.commit()

    sent: list[tuple] = []
    monkeypatch.setattr(
        notifications, "send_email", lambda *a, **k: (sent.append(a), True)[1]
    )
    monkeypatch.setattr(
        notifications.get_settings(), "smtp_host", "smtp.example.com", raising=False
    )
    monkeypatch.setattr(
        notifications.get_settings(), "smtp_from", "saiva@example.com", raising=False
    )

    with SessionLocal() as db:
        household = db.get(models.Household, household_id)
        assert household is not None
        result = notifications.run_for_household(db, household, today=dt.date.today())

    assert result["emailed"] == 1, (
        "an alert that already existed in the feed was never emailed to anybody"
    )
    assert sent, "send_email was not called"


def test_an_alert_already_read_in_the_app_is_not_emailed(
    auth_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chasing someone by email about something they have plainly seen is noise."""
    from app import models
    from app.db import SessionLocal
    from app.services import notifications

    household_id = auth_client.get("/api/auth/me").json()["household"]["id"]
    with SessionLocal() as db:
        notifications.settings_for(db, household_id).email_enabled = True
        db.add(
            models.Notification(
                household_id=household_id,
                key="budget:already-seen",
                type="budget",
                severity="alert",
                title="Seen already",
                body="...",
                read_at=dt.datetime.utcnow(),
            )
        )
        db.commit()
        pending = notifications._pending_email(db, household_id, dt.date.today())
    assert pending == []


def test_a_stale_alert_is_not_emailed(auth_client: TestClient) -> None:
    """Otherwise the first cron run after a quiet month is a wall of old news."""
    from app import models
    from app.db import SessionLocal
    from app.services import notifications

    household_id = auth_client.get("/api/auth/me").json()["household"]["id"]
    old = dt.datetime.utcnow() - dt.timedelta(days=notifications.EMAIL_BACKLOG_DAYS + 5)
    with SessionLocal() as db:
        db.add(
            models.Notification(
                household_id=household_id,
                key="budget:ancient",
                type="budget",
                severity="alert",
                title="Ancient history",
                body="...",
                created_at=old,
            )
        )
        db.commit()
        assert notifications._pending_email(db, household_id, dt.date.today()) == []


# --- The Docker socket ----------------------------------------------------------


def test_nothing_that_serves_http_holds_the_docker_socket() -> None:
    """Container-create access to the daemon is root on the host.

    Watchtower listens for HTTP requests, so a bug in it reached straight through
    to the daemon. The socket now belongs to a filtering proxy that publishes no
    ports and sits on an internal network.
    """
    services = _compose(COMPOSE_PROD)["services"]
    for name, service in services.items():
        mounts = [str(v) for v in (service.get("volumes") or [])]
        holds_socket = any("/var/run/docker.sock" in m for m in mounts)
        if not holds_socket:
            continue
        assert name == "docker-socket-proxy", (
            f"{name} mounts the Docker socket directly; put it behind the proxy"
        )
        assert all(m.endswith(":ro") for m in mounts if "docker.sock" in m), (
            "the proxy should mount the socket read-only"
        )
        assert not service.get("ports"), "the socket proxy must not publish a port"
        assert service.get("networks") == ["docker-api"], (
            "the socket proxy must stay on the isolated network"
        )


def test_the_socket_proxy_refuses_the_dangerous_api_sections() -> None:
    """Blocking exec, volumes and networks is most of what the proxy is for."""
    proxy = _compose(COMPOSE_PROD)["services"]["docker-socket-proxy"]
    env = {k: str(v) for k, v in proxy["environment"].items()}
    for blocked in ("EXEC", "VOLUMES", "NETWORKS", "BUILD", "SWARM", "SYSTEM"):
        assert env.get(blocked) == "0", f"{blocked} is not disabled on the socket proxy"


def test_the_docker_api_network_is_internal() -> None:
    networks = _compose(COMPOSE_PROD)["networks"]
    assert networks["docker-api"]["internal"] is True


# --- HSTS -----------------------------------------------------------------------


def test_hsts_is_set_from_the_certificate_actually_in_use() -> None:
    """A fixed max-age with Caddy's internal CA turns the cert warning into a wall.

    Hard-coding it off instead meant a public deployment never got HSTS at all, so
    the value comes from the deploy, which knows which CA issued the certificate.
    """
    caddyfile = CADDYFILE.read_text()
    assert "Strict-Transport-Security" in caddyfile, "no HSTS header at all"
    assert "{$SAIVA_HSTS_MAX_AGE:0}" in caddyfile, (
        "HSTS should default to 0 and be raised by the deploy, not hard-coded"
    )
    for path in (COMPOSE_PROD, COMPOSE_DEV):
        caddy = _compose(path)["services"]["caddy"]
        assert "SAIVA_HSTS_MAX_AGE" in caddy["environment"], (
            f"{path.name} does not pass SAIVA_HSTS_MAX_AGE to Caddy"
        )


def test_the_deploy_turns_hsts_on_only_for_a_trusted_certificate() -> None:
    deploy = (REPO / "scripts" / "deploy.sh").read_text()
    assert "SAIVA_HSTS_MAX_AGE" in deploy
    assert '"internal"' in deploy, "the deploy no longer distinguishes the internal CA"
