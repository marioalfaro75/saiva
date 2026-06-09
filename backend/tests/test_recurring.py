from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient

TODAY = dt.date.today()


def _ago(days: int) -> str:
    return (TODAY - dt.timedelta(days=days)).isoformat()


def _add(client: TestClient, account_id: str, days_ago: int, cents: int, desc: str) -> None:
    resp = client.post(
        "/api/transactions",
        json={
            "account_id": account_id,
            "txn_date": _ago(days_ago),
            "amount_cents": cents,
            "description": desc,
        },
    )
    assert resp.status_code == 201, resp.text


def _series_by_merchant(client: TestClient) -> dict[str, dict]:
    data = client.get("/api/recurring").json()
    return {s["merchant"]: s for s in data["series"]}


def test_detects_monthly_subscription(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    for d in (150, 120, 90, 60, 30):
        _add(auth_client, account["id"], d, -2500, "ACME GYM MEMBERSHIP")

    series = _series_by_merchant(auth_client)
    assert "Acme Gym Membership" in series or any("Acme Gym" in m for m in series)
    key = next(m for m in series if "Acme Gym" in m)
    s = series[key]
    assert s["cadence"] == "monthly"
    assert s["direction"] == "expense"
    assert s["is_subscription"] is True
    assert s["typical_amount_cents"] == 2500
    assert s["active"] is True


def test_upcoming_bills_lists_next_occurrence(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    for d in (150, 120, 90, 60, 30):
        _add(auth_client, account["id"], d, -4200, "WONDER INTERNET")

    data = auth_client.get("/api/recurring/upcoming", params={"days": 45}).json()
    assert data["horizon_days"] == 45
    merchants = {b["merchant"] for b in data["bills"]}
    assert any("Wonder Internet" in m for m in merchants)
    assert data["total_cents"] >= 4200


def test_detects_recurring_income(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    for d in (56, 42, 28, 14):
        _add(auth_client, account["id"], d, 500000, "EMPLOYER PAY")  # fortnightly pay

    data = auth_client.get("/api/recurring").json()
    income = [s for s in data["series"] if s["direction"] == "income"]
    assert income, "expected a recurring income series"
    assert income[0]["cadence"] == "fortnightly"
    assert data["income_monthly_cents"] > 0


def test_irregular_variable_merchant_not_detected(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    # Scattered dates and wildly varying amounts — a typical grocery run, not a bill.
    for d, c in ((60, -2000), (53, -15000), (39, -3000), (31, -9000), (18, -1000), (9, -12000)):
        _add(auth_client, account["id"], d, c, "GROCER MARKET")

    series = _series_by_merchant(auth_client)
    assert not any("Grocer Market" in m for m in series)


def test_recurring_summary_totals(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    for d in (120, 90, 60, 30):
        _add(auth_client, account["id"], d, -1500, "TINY STREAM CO")
    data = auth_client.get("/api/recurring").json()
    assert data["subscriptions_count"] >= 1
    assert data["monthly_committed_cents"] >= data["subscriptions_monthly_cents"] > 0
