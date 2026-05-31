from __future__ import annotations

from conftest import create_account
from fastapi.testclient import TestClient


def _add(client: TestClient, account_id: str, date: str, cents: int, desc: str) -> dict:
    resp = client.post(
        "/api/transactions",
        json={
            "account_id": account_id,
            "txn_date": date,
            "amount_cents": cents,
            "description": desc,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_transfer_detection_excluded_from_summary(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")
    savings = create_account(auth_client, "Savings", "savings")
    _add(auth_client, everyday["id"], "2025-06-01", 300000, "SALARY ACME PAYROLL")
    _add(auth_client, everyday["id"], "2025-06-02", -20000, "WOOLWORTHS 100 SYDNEY")
    _add(auth_client, everyday["id"], "2025-06-10", -50000, "TRANSFER TO SAVINGS")
    _add(auth_client, savings["id"], "2025-06-10", 50000, "TRANSFER FROM EVERYDAY")

    assert auth_client.post("/api/transactions/detect-transfers").json()["linked"] == 2

    params = {"period": "custom", "start": "2025-06-01", "end": "2025-06-30"}
    summary = auth_client.get("/api/dashboard/summary", params=params).json()
    assert summary["income_cents"] == 300000  # transfer inflow excluded
    assert summary["expense_cents"] == 20000  # transfer outflow excluded
    assert summary["net_cents"] == 280000

    breakdown = auth_client.get("/api/dashboard/categories", params=params).json()
    names = [item["category_name"] for item in breakdown["items"]]
    assert "Supermarkets" in names
    assert "Internal transfers" not in names


def test_trends_has_month_buckets(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    _add(auth_client, account["id"], "2025-04-15", -10000, "WOOLWORTHS")
    _add(auth_client, account["id"], "2025-06-15", -20000, "COLES")
    trends = auth_client.get(
        "/api/dashboard/trends",
        params={"period": "custom", "start": "2025-04-01", "end": "2025-06-30"},
    ).json()
    assert trends["interval"] == "month"
    assert len(trends["points"]) == 3  # Apr, May, Jun
