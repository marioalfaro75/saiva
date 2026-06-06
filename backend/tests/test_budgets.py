from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient


def _categories(client: TestClient) -> list[dict]:
    cats = client.get("/api/categories").json()
    assert cats, "household should have a default category tree after setup"
    return cats


def _leaf_expense(cats: list[dict]) -> dict:
    for c in cats:
        if c["kind"] == "expense" and c["parent_id"]:
            return c
    raise AssertionError("no leaf expense category found")


def _this_month_day() -> str:
    """A date guaranteed to fall inside the current monthly budget window."""
    return dt.date.today().replace(day=1).isoformat()


def _spend(client: TestClient, account_id: str, category_id: str, cents: int) -> None:
    resp = client.post(
        "/api/transactions",
        json={
            "account_id": account_id,
            "txn_date": _this_month_day(),
            "amount_cents": cents,
            "description": "Spend",
            "category_id": category_id,
        },
    )
    assert resp.status_code in (200, 201), resp.text


def test_budget_tracks_spend(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    leaf = _leaf_expense(_categories(auth_client))

    created = auth_client.post(
        "/api/budgets",
        json={"category_id": leaf["id"], "period": "monthly", "limit_cents": 50000},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["category_id"] == leaf["id"]
    assert body["limit_cents"] == 50000
    assert body["actual_cents"] == 0
    assert body["status"] == "ok"

    _spend(auth_client, account["id"], leaf["id"], -30000)

    budgets = auth_client.get("/api/budgets").json()
    assert len(budgets) == 1
    assert budgets[0]["actual_cents"] == 30000
    assert budgets[0]["remaining_cents"] == 20000
    assert budgets[0]["pct_used"] == 0.6
    assert budgets[0]["projected_cents"] >= 30000


def test_budget_over_when_exceeded(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    leaf = _leaf_expense(_categories(auth_client))
    auth_client.post("/api/budgets", json={"category_id": leaf["id"], "limit_cents": 10000})
    _spend(auth_client, account["id"], leaf["id"], -15000)

    budget = auth_client.get("/api/budgets").json()[0]
    assert budget["actual_cents"] == 15000
    assert budget["remaining_cents"] == -5000
    assert budget["status"] == "over"


def test_parent_budget_includes_child_spend(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    cats = _categories(auth_client)
    parents = {c["id"] for c in cats if c["parent_id"] is None and c["kind"] == "expense"}
    child = next(c for c in cats if c["parent_id"] in parents)

    auth_client.post(
        "/api/budgets", json={"category_id": child["parent_id"], "limit_cents": 100000}
    )
    _spend(auth_client, account["id"], child["id"], -25000)

    budget = auth_client.get("/api/budgets").json()[0]
    assert budget["actual_cents"] == 25000  # parent rolled up the child's spend


def test_duplicate_budget_rejected(auth_client: TestClient) -> None:
    leaf = _leaf_expense(_categories(auth_client))
    first = auth_client.post("/api/budgets", json={"category_id": leaf["id"], "limit_cents": 1000})
    assert first.status_code == 201
    dup = auth_client.post("/api/budgets", json={"category_id": leaf["id"], "limit_cents": 2000})
    assert dup.status_code == 409


def test_budget_unknown_category_404(auth_client: TestClient) -> None:
    resp = auth_client.post("/api/budgets", json={"category_id": "nope", "limit_cents": 1000})
    assert resp.status_code == 404


def test_update_and_delete_budget(auth_client: TestClient) -> None:
    leaf = _leaf_expense(_categories(auth_client))
    budget = auth_client.post(
        "/api/budgets", json={"category_id": leaf["id"], "limit_cents": 1000}
    ).json()

    updated = auth_client.patch(
        f"/api/budgets/{budget['id']}", json={"limit_cents": 5000, "period": "annual"}
    )
    assert updated.status_code == 200
    assert updated.json()["limit_cents"] == 5000
    assert updated.json()["period"] == "annual"

    assert auth_client.delete(f"/api/budgets/{budget['id']}").status_code == 204
    assert auth_client.get("/api/budgets").json() == []


def test_budget_periods_have_windows(auth_client: TestClient) -> None:
    expenses = [c for c in _categories(auth_client) if c["kind"] == "expense" and c["parent_id"]]
    for period, category in zip(("monthly", "fortnightly", "annual"), expenses, strict=False):
        body = auth_client.post(
            "/api/budgets",
            json={"category_id": category["id"], "period": period, "limit_cents": 10000},
        ).json()
        assert body["period"] == period
        assert body["period_start"] <= body["period_end"]


def test_demo_seed_includes_budgets(auth_client: TestClient) -> None:
    assert auth_client.post("/api/admin/seed-demo").status_code == 200
    budgets = auth_client.get("/api/budgets").json()
    assert {b["category_name"] for b in budgets} >= {"Supermarkets", "Takeaway", "Coffee"}


def test_viewer_cannot_create_budget(auth_client: TestClient) -> None:
    leaf = _leaf_expense(_categories(auth_client))
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
        resp = viewer.post("/api/budgets", json={"category_id": leaf["id"], "limit_cents": 1000})
        assert resp.status_code == 403
