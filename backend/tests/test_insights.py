from __future__ import annotations

import datetime as dt

from conftest import create_account
from dateutil.relativedelta import relativedelta
from fastapi.testclient import TestClient


def _first_subcategory(client: TestClient) -> dict:
    cats = client.get("/api/categories").json()
    return next(c for c in cats if c["parent_id"])


def _add_txn(client: TestClient, account_id: str, date: dt.date, cents: int, desc: str,
             category_id: str | None = None) -> None:
    body = {
        "account_id": account_id,
        "txn_date": date.isoformat(),
        "amount_cents": cents,
        "description": desc,
    }
    if category_id:
        body["category_id"] = category_id
    assert client.post("/api/transactions", json=body).status_code == 201


def _last_full_month(today: dt.date) -> dt.date:
    return today.replace(day=1) - relativedelta(months=1)


def _month_before(today: dt.date) -> dt.date:
    return today.replace(day=1) - relativedelta(months=2)


def test_top_mover_detected(auth_client: TestClient) -> None:
    sub = _first_subcategory(auth_client)
    account = create_account(auth_client)
    today = dt.date.today()
    _add_txn(auth_client, account["id"], _month_before(today), -10000, "Shop A", sub["id"])
    _add_txn(auth_client, account["id"], _last_full_month(today), -30000, "Shop B", sub["id"])

    data = auth_client.get("/api/insights").json()
    movers = [i for i in data["insights"] if i["type"] == "top_mover"]
    mine = next(i for i in movers if sub["name"] in i["title"])
    assert mine["severity"] == "warn"  # +$200 increase
    assert mine["amount_cents"] == 20000
    assert mine["link"] == f"/transactions?category_id={sub['id']}"


def test_budget_alert_insight(auth_client: TestClient) -> None:
    sub = _first_subcategory(auth_client)
    account = create_account(auth_client)
    budget = auth_client.post(
        "/api/budgets",
        json={"category_id": sub["id"], "period": "monthly", "limit_cents": 1000},
    )
    assert budget.status_code == 201, budget.text
    _add_txn(auth_client, account["id"], dt.date.today(), -5000, "Big spend", sub["id"])

    data = auth_client.get("/api/insights").json()
    alerts = [i for i in data["insights"] if i["type"] == "budget_alert"]
    assert any(i["severity"] == "alert" and sub["name"] in i["title"] for i in alerts)


def test_goal_nudge_insight(auth_client: TestClient) -> None:
    target_date = (dt.date.today() + dt.timedelta(days=90)).isoformat()
    auth_client.post(
        "/api/goals",
        json={"name": "Holiday", "target_cents": 120000, "target_date": target_date},
    )
    data = auth_client.get("/api/insights").json()
    nudges = [i for i in data["insights"] if i["type"] == "goal_nudge"]
    assert any("Holiday" in i["title"] for i in nudges)


def test_fees_insight(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    _add_txn(auth_client, account["id"], _last_full_month(dt.date.today()), -1500,
             "Monthly account fee")
    data = auth_client.get("/api/insights").json()
    fees = [i for i in data["insights"] if i["type"] == "fees"]
    assert fees and fees[0]["amount_cents"] == 1500


def test_duplicate_charge_insight(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    when = _last_full_month(dt.date.today())
    _add_txn(auth_client, account["id"], when, -1999, "Netflix")
    _add_txn(auth_client, account["id"], when, -1999, "Netflix")
    data = auth_client.get("/api/insights").json()
    dups = [i for i in data["insights"] if i["type"] == "duplicate"]
    assert any("Netflix" in i["title"] for i in dups)


def test_insights_structure_on_demo(auth_client: TestClient) -> None:
    assert auth_client.post("/api/admin/seed-demo").status_code == 200
    data = auth_client.get("/api/insights").json()
    assert data["generated_for"]
    assert isinstance(data["insights"], list)
    for insight in data["insights"]:
        assert {"key", "type", "severity", "title", "body"} <= insight.keys()
        assert insight["severity"] in {"alert", "warn", "info"}


def test_insights_requires_auth(client: TestClient) -> None:
    assert client.get("/api/insights").status_code == 401
