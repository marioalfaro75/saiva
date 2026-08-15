"""Importing a statement whose rows span several accounts (PRD R4)."""

from __future__ import annotations

import json

from conftest import create_account
from fastapi.testclient import TestClient

HEADER = "Date,Account,Description,Amount\n"
# The account column is index 1; description 2; amount 3.
MAPPING = {
    "has_header": True, "date_col": 0, "description_col": 2,
    "amount_mode": "single", "amount_col": 3, "account_col": 1,
}
SINGLE_MAPPING = {
    "has_header": True, "date_col": 0, "description_col": 2,
    "amount_mode": "single", "amount_col": 3,
}

ROWS = (
    "01/06/2025,Everyday 062-000 12345678,WOOLWORTHS METRO,-30.00",
    "02/06/2025,Everyday 062-000 12345678,BP CONNECT,-70.00",
    "02/06/2025,Savings 062-000 87654321,INTEREST PAID,1.20",
    "03/06/2025,Visa ****4417,UBER TRIP,-24.50",
)


def _csv(*lines: str) -> bytes:
    return (HEADER + "\n".join(lines or ROWS) + "\n").encode()


def _post(client: TestClient, path: str, content: bytes, **data: str) -> dict:
    resp = client.post(
        f"/api/imports/{path}",
        files={"file": ("multi.csv", content, "text/csv")},
        data={"file_format": "csv", **data},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_sniff_suggests_the_account_column(auth_client: TestClient) -> None:
    sniffed = _post(auth_client, "sniff", _csv())
    assert sniffed["columns"][sniffed["suggested_account_col"]] == "Account"
    # It is only a hint — the mapping itself stays single-account until opted in.
    assert sniffed["suggested_mapping"]["account_col"] is None


def test_sniff_ignores_a_high_cardinality_column(auth_client: TestClient) -> None:
    """A column with a distinct value per row is a reference, not an account."""
    body = "Date,Account Ref,Description,Amount\n" + "\n".join(
        f"0{i}/06/2025,REF-{i},PURCHASE {i},-{i}.00" for i in range(1, 8)
    )
    sniffed = _post(auth_client, "sniff", body.encode())
    assert sniffed["suggested_account_col"] is None


def test_scan_lists_values_and_matches_accounts_by_name(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")
    savings = create_account(auth_client, "Savings", "savings")

    scanned = _post(auth_client, "accounts/scan", _csv(), mapping=json.dumps(MAPPING))
    by_value = {r["value"]: r for r in scanned}
    assert set(by_value) == {
        "Everyday 062-000 12345678", "Savings 062-000 87654321", "Visa ****4417",
    }
    assert by_value["Everyday 062-000 12345678"]["row_count"] == 2
    assert by_value["Everyday 062-000 12345678"]["suggested_account_id"] == everyday["id"]
    assert by_value["Savings 062-000 87654321"]["suggested_account_id"] == savings["id"]
    # Nothing resembles the Visa card yet, so it is left for the user to decide.
    assert by_value["Visa ****4417"]["suggested_account_id"] is None
    assert by_value["Visa ****4417"]["sample_description"] == "UBER TRIP"


def test_scan_requires_an_account_column(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/api/imports/accounts/scan",
        files={"file": ("multi.csv", _csv(), "text/csv")},
        data={"file_format": "csv", "mapping": json.dumps(SINGLE_MAPPING)},
    )
    assert resp.status_code == 400


def test_commit_routes_rows_to_their_accounts(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")
    savings = create_account(auth_client, "Savings", "savings")
    assignments = [
        {"value": "Everyday 062-000 12345678", "account_id": everyday["id"]},
        {"value": "Savings 062-000 87654321", "account_id": savings["id"]},
        {"value": "Visa ****4417", "create": {"name": "Visa", "type": "credit_card"}},
    ]
    result = _post(
        auth_client, "commit", _csv(),
        mapping=json.dumps(MAPPING), assignments=json.dumps(assignments),
    )
    assert result["added"] == 4

    everyday_txns = auth_client.get(
        "/api/transactions", params={"account_id": everyday["id"]}
    ).json()
    assert everyday_txns["total"] == 2
    savings_txns = auth_client.get(
        "/api/transactions", params={"account_id": savings["id"]}
    ).json()
    assert savings_txns["total"] == 1

    # The Visa account was created by the import and holds its one row.
    accounts = auth_client.get("/api/accounts").json()
    visa = next(a for a in accounts if a["name"] == "Visa")
    assert visa["type"] == "credit_card"
    assert auth_client.get(
        "/api/transactions", params={"account_id": visa["id"]}
    ).json()["total"] == 1


def test_unmapped_values_are_skipped_not_misfiled(auth_client: TestClient) -> None:
    """Rows whose account was never mapped must not silently land somewhere wrong."""
    everyday = create_account(auth_client, "Everyday", "everyday")
    assignments = [{"value": "Everyday 062-000 12345678", "account_id": everyday["id"]}]

    preview = _post(
        auth_client, "preview", _csv(),
        mapping=json.dumps(MAPPING), assignments=json.dumps(assignments),
    )
    assert preview["unassigned_count"] == 2
    unassigned = [r for r in preview["rows"] if r["status"] == "unassigned"]
    assert all(r["will_import"] is False and r["account_id"] is None for r in unassigned)

    result = _post(
        auth_client, "commit", _csv(),
        mapping=json.dumps(MAPPING), assignments=json.dumps(assignments),
    )
    assert (result["added"], result["skipped"]) == (2, 2)
    assert auth_client.get("/api/transactions").json()["total"] == 2


def test_explicit_skip_excludes_an_account(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")
    assignments = [
        {"value": "Everyday 062-000 12345678", "account_id": everyday["id"]},
        {"value": "Savings 062-000 87654321", "skip": True},
        {"value": "Visa ****4417", "skip": True},
    ]
    result = _post(
        auth_client, "commit", _csv(),
        mapping=json.dumps(MAPPING), assignments=json.dumps(assignments),
    )
    assert (result["added"], result["skipped"]) == (2, 2)


def test_preview_groups_by_account_without_creating_anything(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")
    assignments = [
        {"value": "Everyday 062-000 12345678", "account_id": everyday["id"]},
        {"value": "Savings 062-000 87654321", "skip": True},
        {"value": "Visa ****4417", "create": {"name": "Visa", "type": "credit_card"}},
    ]
    preview = _post(
        auth_client, "preview", _csv(),
        mapping=json.dumps(MAPPING), assignments=json.dumps(assignments),
    )
    summaries = {a["account_name"]: a for a in preview["accounts"]}
    assert summaries["Everyday"]["new_count"] == 2
    assert summaries["Visa"]["new_count"] == 1
    assert summaries["Visa"]["account_id"] is None  # proposed, not yet created
    assert preview["account_id"] is None  # the batch spans accounts

    # A preview must not have created the Visa account.
    assert [a for a in auth_client.get("/api/accounts").json() if a["name"] == "Visa"] == []


def test_dedup_is_per_account(auth_client: TestClient) -> None:
    """The same amount and description on the same day in two different accounts are
    two separate transactions, not a duplicate."""
    everyday = create_account(auth_client, "Everyday", "everyday")
    savings = create_account(auth_client, "Savings", "savings")
    rows = (
        "01/06/2025,Everyday 062-000 12345678,BANK FEE,-5.00",
        "01/06/2025,Savings 062-000 87654321,BANK FEE,-5.00",
    )
    assignments = [
        {"value": "Everyday 062-000 12345678", "account_id": everyday["id"]},
        {"value": "Savings 062-000 87654321", "account_id": savings["id"]},
    ]
    result = _post(
        auth_client, "commit", _csv(*rows),
        mapping=json.dumps(MAPPING), assignments=json.dumps(assignments),
    )
    assert result["added"] == 2

    # …and re-importing still skips both, per account.
    again = _post(
        auth_client, "commit", _csv(*rows),
        mapping=json.dumps(MAPPING), assignments=json.dumps(assignments),
    )
    assert (again["added"], again["skipped"]) == (0, 2)


def test_mapping_is_remembered_for_the_next_import(auth_client: TestClient) -> None:
    everyday = create_account(auth_client, "Everyday", "everyday")
    savings = create_account(auth_client, "Savings", "savings")
    _post(
        auth_client, "commit", _csv(),
        mapping=json.dumps(MAPPING),
        assignments=json.dumps([
            # Deliberately cross-assign so the suggestion can only come from memory,
            # not from matching the value against the account name.
            {"value": "Everyday 062-000 12345678", "account_id": savings["id"]},
            {"value": "Savings 062-000 87654321", "account_id": everyday["id"]},
        ]),
    )
    scanned = _post(auth_client, "accounts/scan", _csv(), mapping=json.dumps(MAPPING))
    by_value = {r["value"]: r["suggested_account_id"] for r in scanned}
    assert by_value["Everyday 062-000 12345678"] == savings["id"]
    assert by_value["Savings 062-000 87654321"] == everyday["id"]


def test_single_account_import_still_works(auth_client: TestClient) -> None:
    """The ordinary one-account-per-file path is unchanged."""
    account = create_account(auth_client)
    result = _post(
        auth_client, "commit", _csv(),
        account_id=account["id"], mapping=json.dumps(SINGLE_MAPPING),
    )
    assert result["added"] == 4


def test_single_account_import_requires_an_account(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/api/imports/commit",
        files={"file": ("multi.csv", _csv(), "text/csv")},
        data={"file_format": "csv", "mapping": json.dumps(SINGLE_MAPPING)},
    )
    assert resp.status_code == 400
