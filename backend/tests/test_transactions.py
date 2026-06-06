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


def test_recategorise_learns_rule_and_applies_to_similar(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    first = _add(auth_client, account["id"], "2025-06-01", -1500, "MYSTERY BAKERY 42")
    second = _add(auth_client, account["id"], "2025-06-08", -1700, "MYSTERY BAKERY 42")

    categories = auth_client.get("/api/categories").json()
    coffee_id = next(c["id"] for c in categories if c["name"] == "Coffee")

    resp = auth_client.post(
        f"/api/transactions/{first['id']}/recategorise",
        json={"category_id": coffee_id, "make_rule": True, "scope": "merchant"},
    )
    assert resp.status_code == 200
    assert resp.json()["transaction"]["category_name"] == "Coffee"
    assert resp.json()["updated_count"] == 2  # this row plus the sibling

    # The sibling transaction was updated too.
    other = auth_client.get("/api/transactions", params={"q": "mystery"}).json()
    assert all(item["category_name"] == "Coffee" for item in other["items"])
    assert second["id"] in {item["id"] for item in other["items"]}

    # A user rule now exists for that merchant.
    rules = auth_client.get("/api/rules").json()
    assert any(rule["category_id"] == coffee_id and rule["source"] == "user" for rule in rules)


def test_split_keeps_balance_and_validates_sum(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    categories = auth_client.get("/api/categories").json()
    groceries = next(c["id"] for c in categories if c["name"] == "Supermarkets")
    alcohol = next(c["id"] for c in categories if c["name"] == "Alcohol/Bars")
    parent = _add(auth_client, account["id"], "2025-06-01", -10000, "WOOLWORTHS BIG SHOP")

    bad = auth_client.post(
        f"/api/transactions/{parent['id']}/split",
        json={"splits": [{"amount_cents": -7000, "category_id": groceries}]},
    )
    assert bad.status_code == 422  # need at least two splits

    mismatched = auth_client.post(
        f"/api/transactions/{parent['id']}/split",
        json={
            "splits": [
                {"amount_cents": -7000, "category_id": groceries},
                {"amount_cents": -1000, "category_id": alcohol},
            ]
        },
    )
    assert mismatched.status_code == 400  # must sum to the original

    ok = auth_client.post(
        f"/api/transactions/{parent['id']}/split",
        json={
            "splits": [
                {"amount_cents": -7000, "category_id": groceries},
                {"amount_cents": -3000, "category_id": alcohol},
            ]
        },
    )
    assert ok.status_code == 200
    assert len(ok.json()) == 2

    # Balance still reflects the single parent line, not the children.
    assert auth_client.get("/api/accounts").json()[0]["balance_cents"] == -10000
