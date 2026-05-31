from __future__ import annotations

from fastapi.testclient import TestClient


def test_update_household_pay_cycle(auth_client: TestClient) -> None:
    resp = auth_client.patch(
        "/api/household",
        json={"period_basis": "fortnightly", "pay_cycle_anchor": "2025-01-02", "state": "VIC"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["period_basis"] == "fortnightly"
    assert body["state"] == "VIC"

    # The pay-cycle period selector now resolves to a 14-day window.
    summary = auth_client.get("/api/dashboard/summary", params={"period": "this_period"}).json()
    from datetime import date

    span = (date.fromisoformat(summary["end"]) - date.fromisoformat(summary["start"])).days
    assert span == 13


def test_create_category_rule_and_delete(auth_client: TestClient) -> None:
    categories = auth_client.get("/api/categories").json()
    shopping = next(c["id"] for c in categories if c["name"] == "Shopping")

    created = auth_client.post(
        "/api/categories",
        json={"name": "Bike parts", "parent_id": shopping, "kind": "expense"},
    )
    assert created.status_code == 201
    category_id = created.json()["id"]

    rule = auth_client.post(
        "/api/rules",
        json={"match_type": "contains", "pattern": "99 bikes", "category_id": category_id},
    )
    assert rule.status_code == 201
    rule_id = rule.json()["id"]

    assert auth_client.delete(f"/api/rules/{rule_id}").status_code == 204
    remaining = [r["id"] for r in auth_client.get("/api/rules").json()]
    assert rule_id not in remaining
