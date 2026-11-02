from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient


def _add(
    client: TestClient, today: dt.date, account_id: str, days_ago: int, cents: int, desc: str
) -> None:
    resp = client.post(
        "/api/transactions",
        json={
            "account_id": account_id,
            "txn_date": (today - dt.timedelta(days=days_ago)).isoformat(),
            "amount_cents": cents,
            "description": desc,
        },
    )
    assert resp.status_code == 201, resp.text


def _seed(client: TestClient, today: dt.date) -> str:
    """Seed relative to the date the asserting test captured, not to a fresh one.

    The seeding and the assertions have to agree about which day it is. Calling
    dt.date.today() again in here would reintroduce the same disagreement this change
    exists to remove, just across a narrower window.
    """
    account = create_account(client)
    # Fortnightly salary -> detected recurring income.
    for d in (56, 42, 28, 14):
        _add(client, today, account["id"], d, 500000, "EMPLOYER SALARY")
    # Uncategorised everyday spend across the baseline window.
    for d in (80, 60, 40, 20, 10, 3):
        _add(client, today, account["id"], d, -6000, "MYSTERYVENDOR XYZ")
    return account["id"]


def test_forecast_shape_and_income(auth_client: TestClient, today: dt.date) -> None:
    _seed(auth_client, today)
    data = auth_client.post("/api/forecast", json={"days": 90}).json()
    assert data["horizon_days"] == 90
    assert data["points"][0]["date"] == today.isoformat()
    assert data["points"][-1]["date"] == (today + dt.timedelta(days=90)).isoformat()
    assert len(data["points"]) >= 2
    assert data["monthly_income_cents"] > 0  # fortnightly salary detected
    assert data["monthly_expense_cents"] > 0
    # Daily spend pulls the balance below the starting point at some stage.
    assert data["low_balance_cents"] <= data["starting_balance_cents"]


def test_forecast_whatif_cuts_spend(auth_client: TestClient, today: dt.date) -> None:
    _seed(auth_client, today)
    base = auth_client.post("/api/forecast", json={"days": 90}).json()
    # Cut the (uncategorised) everyday spend entirely.
    whatif = auth_client.post(
        "/api/forecast",
        json={"days": 90, "adjustments": [{"category_id": None, "pct": -100}]},
    ).json()
    assert whatif["monthly_expense_cents"] < base["monthly_expense_cents"]
    assert whatif["end_balance_cents"] >= base["end_balance_cents"]


def test_forecast_defaults_without_body(auth_client: TestClient, today: dt.date) -> None:
    _seed(auth_client, today)
    resp = auth_client.post("/api/forecast")
    assert resp.status_code == 200
    assert resp.json()["horizon_days"] == 90
