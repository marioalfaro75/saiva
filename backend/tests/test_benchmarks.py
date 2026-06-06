from __future__ import annotations

import datetime as dt

from conftest import create_account
from dateutil.relativedelta import relativedelta
from fastapi.testclient import TestClient


def _category(client: TestClient, name: str) -> dict:
    cats = client.get("/api/categories").json()
    return next(c for c in cats if c["name"] == name)


def _last_full_month(today: dt.date) -> dt.date:
    return today.replace(day=1) - relativedelta(months=1)


def test_benchmark_structure(auth_client: TestClient) -> None:
    data = auth_client.get("/api/benchmarks").json()
    assert data["note"] and "ABS" in data["note"]
    assert data["typical_total_weekly_cents"] > 0
    assert data["your_total_weekly_cents"] == 0  # no spend yet
    names = {i["category"] for i in data["items"]}
    assert {"Housing", "Groceries", "Transport"} <= names
    for item in data["items"]:
        assert item["typical_weekly_cents"] > 0
        assert item["your_weekly_cents"] == 0


def test_benchmark_rolls_up_to_top_level(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    supermarkets = _category(auth_client, "Supermarkets")  # child of Groceries
    when = _last_full_month(dt.date.today())
    for _ in range(2):
        auth_client.post(
            "/api/transactions",
            json={
                "account_id": account["id"],
                "txn_date": when.isoformat(),
                "amount_cents": -20000,
                "description": "Woolworths",
                "category_id": supermarkets["id"],
            },
        )
    data = auth_client.get("/api/benchmarks").json()
    g = next(i for i in data["items"] if i["category"] == "Groceries")
    # $400 over one month -> ~$92.31/week
    assert 9000 <= g["your_weekly_cents"] <= 9500
    assert g["diff_cents"] == g["your_weekly_cents"] - g["typical_weekly_cents"]
    assert data["your_total_weekly_cents"] >= g["your_weekly_cents"]


def test_benchmark_scales_with_household_size(auth_client: TestClient) -> None:
    base = auth_client.get("/api/benchmarks").json()
    assert base["adults"] == 1 and base["children"] == 0
    patched = auth_client.patch("/api/household", json={"adults": 2, "children": 2})
    assert patched.status_code == 200
    bigger = auth_client.get("/api/benchmarks").json()
    assert bigger["adults"] == 2 and bigger["children"] == 2
    assert bigger["typical_total_weekly_cents"] > base["typical_total_weekly_cents"]


def test_benchmark_requires_auth(client: TestClient) -> None:
    assert client.get("/api/benchmarks").status_code == 401
