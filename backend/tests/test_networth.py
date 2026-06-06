from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient


def _asset(client: TestClient, name: str = "Family home", value: int = 85000000):
    return client.post(
        "/api/net-worth/items", json={"name": name, "kind": "asset", "value_cents": value}
    )


def _liability(client: TestClient, name: str = "Mortgage", value: int = 52000000):
    return client.post(
        "/api/net-worth/items", json={"name": name, "kind": "liability", "value_cents": value}
    )


def test_net_worth_totals(auth_client: TestClient) -> None:
    assert _asset(auth_client).status_code == 201
    body = _liability(auth_client).json()
    assert body["assets_cents"] == 85000000
    assert body["liabilities_cents"] == 52000000
    assert body["net_cents"] == 33000000
    assert len(body["items"]) == 2


def test_history_records_today(auth_client: TestClient) -> None:
    _asset(auth_client, value=1000)
    nw = auth_client.get("/api/net-worth").json()
    assert nw["history"]
    assert nw["history"][-1]["as_of"] == dt.date.today().isoformat()
    assert nw["history"][-1]["net_cents"] == 1000


def test_update_item_changes_totals(auth_client: TestClient) -> None:
    item = _asset(auth_client, value=1000).json()["items"][0]
    updated = auth_client.patch(f"/api/net-worth/items/{item['id']}", json={"value_cents": 5000})
    assert updated.status_code == 200
    assert updated.json()["assets_cents"] == 5000
    assert updated.json()["net_cents"] == 5000


def test_delete_item(auth_client: TestClient) -> None:
    item_id = _asset(auth_client, value=1000).json()["items"][0]["id"]
    res = auth_client.delete(f"/api/net-worth/items/{item_id}")
    assert res.status_code == 200
    assert res.json()["assets_cents"] == 0
    assert res.json()["items"] == []


def test_snapshot_endpoint(auth_client: TestClient) -> None:
    _asset(auth_client, value=2000)
    res = auth_client.post("/api/net-worth/snapshot")
    assert res.status_code == 200
    assert res.json()["history"][-1]["net_cents"] == 2000


def test_unknown_item_404(auth_client: TestClient) -> None:
    bad_patch = auth_client.patch("/api/net-worth/items/nope", json={"value_cents": 1})
    assert bad_patch.status_code == 404
    assert auth_client.delete("/api/net-worth/items/nope").status_code == 404


def test_invalid_kind_rejected(auth_client: TestClient) -> None:
    res = auth_client.post(
        "/api/net-worth/items", json={"name": "X", "kind": "bogus", "value_cents": 1}
    )
    assert res.status_code == 422


def test_demo_seed_includes_net_worth(auth_client: TestClient) -> None:
    assert auth_client.post("/api/admin/seed-demo").status_code == 200
    nw = auth_client.get("/api/net-worth").json()
    assert nw["assets_cents"] > nw["liabilities_cents"] > 0
    assert nw["net_cents"] > 0
    assert len(nw["history"]) >= 2  # several monthly snapshots
    assert any(i["name"] == "Mortgage" for i in nw["items"])


def test_viewer_cannot_add_item(auth_client: TestClient) -> None:
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
        res = viewer.post(
            "/api/net-worth/items", json={"name": "X", "kind": "asset", "value_cents": 1}
        )
        assert res.status_code == 403
