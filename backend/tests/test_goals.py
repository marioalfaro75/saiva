from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient


def test_goal_progress_manual(auth_client: TestClient) -> None:
    created = auth_client.post(
        "/api/goals", json={"name": "Car", "target_cents": 100000, "current_cents": 25000}
    )
    assert created.status_code == 201, created.text
    goal = created.json()
    assert goal["current_cents"] == 25000
    assert goal["remaining_cents"] == 75000
    assert goal["pct_complete"] == 0.25
    assert goal["complete"] is False
    assert goal["suggested_per_period_cents"] == 0  # no target date


def test_goal_linked_account_uses_balance(auth_client: TestClient) -> None:
    account = create_account(auth_client, name="Savings", type_="savings")
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": "2026-01-01",
            "amount_cents": 60000,
            "description": "Deposit",
        },
    )
    goal = auth_client.post(
        "/api/goals",
        json={"name": "Buffer", "target_cents": 100000, "account_id": account["id"]},
    ).json()
    assert goal["account_id"] == account["id"]
    assert goal["account_name"] == "Savings"
    assert goal["current_cents"] == 60000
    assert goal["remaining_cents"] == 40000


def test_goal_suggested_contribution(auth_client: TestClient) -> None:
    target_date = (dt.date.today() + dt.timedelta(days=120)).isoformat()
    goal = auth_client.post(
        "/api/goals",
        json={"name": "Holiday", "target_cents": 120000, "target_date": target_date},
    ).json()
    assert goal["period_label"] == "month"
    assert 0 < goal["suggested_per_period_cents"] <= goal["remaining_cents"]


def test_goal_complete_caps_pct(auth_client: TestClient) -> None:
    goal = auth_client.post(
        "/api/goals", json={"name": "Done", "target_cents": 10000, "current_cents": 20000}
    ).json()
    assert goal["complete"] is True
    assert goal["pct_complete"] == 1.0
    assert goal["remaining_cents"] == 0
    assert goal["suggested_per_period_cents"] == 0


def test_update_and_delete_goal(auth_client: TestClient) -> None:
    goal = auth_client.post("/api/goals", json={"name": "X", "target_cents": 10000}).json()
    updated = auth_client.patch(
        f"/api/goals/{goal['id']}", json={"current_cents": 5000, "target_cents": 20000}
    )
    assert updated.status_code == 200
    assert updated.json()["current_cents"] == 5000
    assert updated.json()["target_cents"] == 20000
    assert auth_client.delete(f"/api/goals/{goal['id']}").status_code == 204
    assert auth_client.get("/api/goals").json() == []


def test_goal_unknown_account_404(auth_client: TestClient) -> None:
    res = auth_client.post(
        "/api/goals", json={"name": "X", "target_cents": 1000, "account_id": "nope"}
    )
    assert res.status_code == 404


def test_demo_seed_includes_goals(auth_client: TestClient) -> None:
    assert auth_client.post("/api/admin/seed-demo").status_code == 200
    names = {g["name"] for g in auth_client.get("/api/goals").json()}
    assert {"Emergency fund", "Holiday"} <= names


def test_viewer_cannot_create_goal(auth_client: TestClient) -> None:
    auth_client.post(
        "/api/household/users",
        json={"name": "Viewer", "email": "viewer@example.com",
              "password": "viewerpass99", "role": "viewer"},
    )
    from fastapi.testclient import TestClient as TC

    from app.main import app

    with TC(app) as viewer:
        viewer.headers["X-CSRF-Token"] = viewer.get("/api/auth/csrf").json()["csrf_token"]
        login = viewer.post(
            "/api/auth/login", json={"email": "viewer@example.com", "password": "viewerpass99"}
        )
        viewer.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        res = viewer.post("/api/goals", json={"name": "X", "target_cents": 1000})
        assert res.status_code == 403
