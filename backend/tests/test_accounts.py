from __future__ import annotations

from conftest import create_account
from fastapi.testclient import TestClient


def test_account_balance_reflects_transactions(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": "2025-06-01",
            "amount_cents": -2500,
            "description": "Local cafe",
        },
    )
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": "2025-06-02",
            "amount_cents": 10000,
            "description": "Refund",
        },
    )
    accounts = auth_client.get("/api/accounts").json()
    assert accounts[0]["balance_cents"] == 7500
    assert accounts[0]["txn_count"] == 2


def test_delete_account_blocked_with_transactions(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": "2025-06-01",
            "amount_cents": -2500,
            "description": "Local cafe",
        },
    )
    assert auth_client.delete(f"/api/accounts/{account['id']}").status_code == 409


def test_viewer_cannot_write(auth_client: TestClient) -> None:
    # Owner creates a viewer; viewer logs in on a separate client.
    auth_client.post(
        "/api/household/users",
        json={
            "name": "Viewer",
            "email": "viewer@example.com",
            "password": "viewerpass99",
            "role": "viewer",
        },
    )
    from fastapi.testclient import TestClient as TC

    from app.main import app

    with TC(app) as viewer:
        viewer.headers["X-CSRF-Token"] = viewer.get("/api/auth/csrf").json()["csrf_token"]
        login = viewer.post(
            "/api/auth/login",
            json={"email": "viewer@example.com", "password": "viewerpass99"},
        )
        assert login.status_code == 200
        viewer.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        resp = viewer.post("/api/accounts", json={"name": "Nope", "type": "everyday"})
        assert resp.status_code == 403
