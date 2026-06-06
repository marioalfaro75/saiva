from __future__ import annotations

from conftest import create_account
from fastapi.testclient import TestClient


def _add(
    client: TestClient, account_id: str, desc: str, cents: int = -1000, date: str = "2025-06-01"
) -> dict:
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


def _cat(client: TestClient, name: str) -> str:
    return next(c["id"] for c in client.get("/api/categories").json() if c["name"] == name)


def _make_rule(client: TestClient, pattern: str, category_id: str, match_type: str = "contains"):
    return client.post(
        "/api/rules",
        json={"match_type": match_type, "pattern": pattern, "category_id": category_id},
    )


def _items_by_id(client: TestClient, q: str) -> dict:
    items = client.get("/api/transactions", params={"q": q}).json()["items"]
    return {i["id"]: i for i in items}


def test_apply_to_similar_by_exact_description(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    a = _add(auth_client, account["id"], "ACME PTY LTD")
    _add(auth_client, account["id"], "ACME PTY LTD")
    _add(auth_client, account["id"], "ACME PTY LTD DIFFERENT")
    coffee = _cat(auth_client, "Coffee")

    resp = auth_client.post(
        f"/api/transactions/{a['id']}/recategorise",
        json={"category_id": coffee, "scope": "exact"},
    )
    assert resp.status_code == 200
    assert resp.json()["updated_count"] == 2  # the two identical rows only

    items = auth_client.get("/api/transactions", params={"q": "ACME"}).json()["items"]
    by_desc = {i["raw_description"]: i["category_name"] for i in items}
    assert by_desc["ACME PTY LTD"] == "Coffee"
    assert by_desc["ACME PTY LTD DIFFERENT"] is None


def test_lock_exempts_from_apply_to_similar(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    a = _add(auth_client, account["id"], "DUET CAFE")
    b = _add(auth_client, account["id"], "DUET CAFE")
    coffee = _cat(auth_client, "Coffee")

    # Lock b on its own, leaving it uncategorised.
    locked = auth_client.patch(f"/api/transactions/{b['id']}", json={"category_locked": True})
    assert locked.status_code == 200
    assert locked.json()["category_locked"] is True

    # Apply-to-similar from a should skip the locked sibling.
    resp = auth_client.post(
        f"/api/transactions/{a['id']}/recategorise",
        json={"category_id": coffee, "scope": "merchant"},
    )
    assert resp.json()["updated_count"] == 1  # only a

    items = _items_by_id(auth_client, "DUET")
    assert items[a["id"]]["category_name"] == "Coffee"
    assert items[b["id"]]["category_name"] is None


def test_bulk_categorise_selected(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    a = _add(auth_client, account["id"], "ONE")
    b = _add(auth_client, account["id"], "TWO")
    coffee = _cat(auth_client, "Coffee")

    resp = auth_client.post(
        "/api/transactions/bulk-categorise",
        json={"ids": [a["id"], b["id"]], "category_id": coffee},
    )
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2
    items = auth_client.get("/api/transactions").json()["items"]
    assert all(i["category_name"] == "Coffee" for i in items if i["id"] in {a["id"], b["id"]})


def test_groups_endpoint_groups_uncategorised(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    for _ in range(3):
        _add(auth_client, account["id"], "REPEATING MERCHANT")
    _add(auth_client, account["id"], "ONE OFF")

    groups = auth_client.get("/api/transactions/groups", params={"by": "merchant"}).json()
    assert groups["by"] == "merchant"
    top = max(groups["groups"], key=lambda g: g["count"])
    assert top["count"] == 3


def test_rule_preview_and_apply_backfill(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    # Descriptions with no built-in starter rule, so they start uncategorised.
    _add(auth_client, account["id"], "ZUNKO MEDIA P0123")
    _add(auth_client, account["id"], "ZUNKO MEDIA P0456")
    streaming = _cat(auth_client, "Streaming")

    preview = auth_client.post(
        "/api/rules/preview", json={"match_type": "contains", "pattern": "zunko"}
    ).json()
    assert preview["matched"] == 2
    assert preview["fillable"] == 2
    assert preview["samples"]

    rule = _make_rule(auth_client, "zunko", streaming)
    assert rule.status_code == 201
    applied = auth_client.post(f"/api/rules/{rule.json()['id']}/apply").json()
    assert applied["updated"] == 2
    items = auth_client.get("/api/transactions", params={"q": "zunko"}).json()["items"]
    assert all(i["category_name"] == "Streaming" for i in items)


def test_backfill_skips_locked_and_categorised(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    locked_txn = _add(auth_client, account["id"], "ZONK SUB")
    plain = _add(auth_client, account["id"], "ZONK SUB")
    streaming = _cat(auth_client, "Streaming")
    auth_client.patch(f"/api/transactions/{locked_txn['id']}", json={"category_locked": True})

    rule = _make_rule(auth_client, "zonk", streaming).json()
    applied = auth_client.post(f"/api/rules/{rule['id']}/apply").json()
    assert applied["updated"] == 1  # only the unlocked one

    items = _items_by_id(auth_client, "zonk")
    assert items[plain["id"]]["category_name"] == "Streaming"
    assert items[locked_txn["id"]]["category_name"] is None


def test_update_rule_and_toggle_active(auth_client: TestClient) -> None:
    streaming = _cat(auth_client, "Streaming")
    rule = auth_client.post(
        "/api/rules", json={"match_type": "contains", "pattern": "stan", "category_id": streaming}
    ).json()
    patched = auth_client.patch(
        f"/api/rules/{rule['id']}", json={"pattern": "stan.com.au", "is_active": False}
    )
    assert patched.status_code == 200
    assert patched.json()["pattern"] == "stan.com.au"
    assert patched.json()["is_active"] is False


def test_make_rule_creates_equals_rule_for_exact_scope(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    txn = _add(auth_client, account["id"], "WEIRD DIRECT DEBIT 999")
    coffee = _cat(auth_client, "Coffee")
    auth_client.post(
        f"/api/transactions/{txn['id']}/recategorise",
        json={"category_id": coffee, "scope": "exact", "make_rule": True},
    )
    rules = auth_client.get("/api/rules").json()
    assert any(r["match_type"] == "equals" and r["source"] == "user" for r in rules)
