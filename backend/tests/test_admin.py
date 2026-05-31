from __future__ import annotations

from fastapi.testclient import TestClient


def test_seed_demo_export_and_reseed_blocked(auth_client: TestClient) -> None:
    seeded = auth_client.post("/api/admin/seed-demo")
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["transactions"] > 50

    # The dashboard now has real numbers to show.
    summary = auth_client.get("/api/dashboard/summary", params={"period": "last_90d"}).json()
    assert summary["income_cents"] > 0
    assert summary["expense_cents"] > 0

    # Full data export (no lock-in).
    export = auth_client.get("/api/admin/export").json()
    assert len(export["accounts"]) == 3
    assert len(export["transactions"]) > 50
    assert export["household"]["currency"] == "AUD"

    # Re-seeding is refused once the household has accounts.
    assert auth_client.post("/api/admin/seed-demo").status_code == 409

    # Audit log captured the seeding.
    actions = [entry["action"] for entry in auth_client.get("/api/admin/audit").json()]
    assert "seed_demo" in actions
