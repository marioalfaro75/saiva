from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient

TODAY = dt.date.today().isoformat()


def _cat(client: TestClient, name: str) -> str:
    return next(c["id"] for c in client.get("/api/categories").json() if c["name"] == name)


def _add(client: TestClient, account_id: str, cents: int, desc: str) -> dict:
    resp = client.post(
        "/api/transactions",
        json={
            "account_id": account_id,
            "txn_date": TODAY,
            "amount_cents": cents,
            "description": desc,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_large_transaction_alert_and_dedupe(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    _add(auth_client, account["id"], -60000, "BIG TV PURCHASE")  # over the 50,000 default

    data = auth_client.get("/api/notifications").json()
    assert "large_txn" in {n["type"] for n in data["items"]}
    count = len(data["items"])

    # Regenerating on the next read must not duplicate.
    again = auth_client.get("/api/notifications").json()
    assert len(again["items"]) == count


def test_budget_over_alert(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    coffee = _cat(auth_client, "Coffee")
    auth_client.post(
        "/api/budgets", json={"category_id": coffee, "period": "monthly", "limit_cents": 1000}
    )
    txn = _add(auth_client, account["id"], -5000, "CORNER COFFEE")
    auth_client.post(
        f"/api/transactions/{txn['id']}/recategorise",
        json={"category_id": coffee, "scope": "none"},
    )
    items = auth_client.get("/api/notifications").json()["items"]
    assert any(n["type"] == "budget" and "Coffee" in n["title"] for n in items)


def test_mark_read_reduces_unread(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    _add(auth_client, account["id"], -70000, "BIG APPLIANCE")
    data = auth_client.get("/api/notifications").json()
    assert data["unread"] >= 1
    note_id = data["items"][0]["id"]

    auth_client.post(f"/api/notifications/{note_id}/read")
    after = auth_client.get("/api/notifications").json()
    assert after["unread"] == data["unread"] - 1


def test_mark_all_read(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    _add(auth_client, account["id"], -60000, "BIG ONE")
    _add(auth_client, account["id"], -80000, "BIG TWO")
    auth_client.get("/api/notifications")  # generate the feed
    resp = auth_client.post("/api/notifications/read-all")
    assert resp.status_code == 200
    assert auth_client.get("/api/notifications").json()["unread"] == 0


def test_settings_roundtrip(auth_client: TestClient) -> None:
    base = auth_client.get("/api/notifications/settings").json()
    assert base["smtp_configured"] is False
    assert base["email_enabled"] is False

    upd = auth_client.patch(
        "/api/notifications/settings",
        json={"email_enabled": True, "digest": "weekly", "large_txn_threshold_cents": 100000},
    )
    assert upd.status_code == 200
    assert upd.json()["digest"] == "weekly"
    assert upd.json()["large_txn_threshold_cents"] == 100000


def test_run_requires_token(auth_client: TestClient) -> None:
    # No NOTIFICATIONS_TOKEN configured in tests -> always unauthorized.
    assert auth_client.post("/api/notifications/run").status_code == 401


def test_test_email_requires_smtp(auth_client: TestClient) -> None:
    assert auth_client.post("/api/notifications/test").status_code == 400
