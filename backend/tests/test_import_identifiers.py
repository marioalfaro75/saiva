"""Remembering which account a statement value refers to.

A file names its accounts the bank's way — an account number, an OFX ACCTID — and a
person has to say once which of theirs that is. After that it should be recognised,
including from a file of a different format, and including after other imports have
happened in between.
"""

from __future__ import annotations

import json

from conftest import create_account
from fastapi.testclient import TestClient

HEADER = "Date,Account,Description,Amount\n"
MAPPING = {
    "has_header": True, "date_col": 0, "description_col": 2,
    "amount_mode": "single", "amount_col": 3, "account_col": 1,
}


def _csv(account_value: str) -> bytes:
    return (HEADER + f"01/06/2025,{account_value},WOOLWORTHS METRO,-30.00\n").encode()


def _post(client: TestClient, path: str, content: bytes, **data: str):
    resp = client.post(
        f"/api/imports/{path}",
        files={"file": ("s.csv", content, "text/csv")},
        data={"file_format": "csv", **data},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _commit(client: TestClient, value: str, assignments: list[dict]) -> dict:
    return _post(
        client,
        "commit",
        _csv(value),
        mapping=json.dumps(MAPPING),
        assignments=json.dumps(assignments),
        account_id="",
    )


def test_a_mapped_account_number_survives_an_unrelated_import(
    auth_client: TestClient,
) -> None:
    """The older mechanism remembered only the most recent multi-account import, so
    importing anything else in between lost the mapping. Binding the identifier to the
    account itself is what makes it durable."""
    mortgage = create_account(auth_client, name="Mortgage", type_="home_loan")
    assert _commit(auth_client, "046568586577", [
        {"value": "046568586577", "account_id": mortgage["id"]}
    ])["added"] == 1

    # A different multi-account file, which displaces the remembered map.
    card = create_account(auth_client, name="Visa", type_="credit_card")
    _commit(auth_client, "999000111222", [
        {"value": "999000111222", "account_id": card["id"]}
    ])

    found = _post(
        auth_client, "accounts/scan", _csv("046568586577"), mapping=json.dumps(MAPPING)
    )
    assert found[0]["suggested_account_id"] == mortgage["id"]


def test_a_number_excel_has_mangled_is_not_remembered(auth_client: TestClient) -> None:
    """"7.34364E+11" has lost its digits, so a clean export later would not match it.
    It maps for the file in hand and is then forgotten, rather than binding the account
    to a value that can never recur."""
    account = create_account(auth_client)
    _commit(auth_client, "7.34364E+11", [
        {"value": "7.34364E+11", "account_id": account["id"]}
    ])
    accounts = auth_client.get("/api/accounts").json()
    assert [a for a in accounts if a["id"] == account["id"]]

    found = _post(
        auth_client, "accounts/scan", _csv("7.34364E+11"), mapping=json.dumps(MAPPING)
    )
    assert found[0]["looks_mangled"] is True


def test_a_short_fragment_is_not_remembered(auth_client: TestClient) -> None:
    """A card's last four is not unique enough to bind an account to — another card
    ending 5263 would silently inherit it."""
    account = create_account(auth_client)
    _commit(auth_client, "5263", [{"value": "5263", "account_id": account["id"]}])
    found = _post(auth_client, "accounts/scan", _csv("5263"), mapping=json.dumps(MAPPING))
    # Still guessable by name similarity, but not bound by identifier.
    assert found[0]["looks_mangled"] is False


def test_the_scan_describes_each_account_well_enough_to_recognise(
    auth_client: TestClient,
) -> None:
    body = (
        b"Date,Account,Description,Amount,Balance\n"
        b"01/06/2025,046568586577,OPENING,-100.00,-819580.37\n"
        b"02/06/2025,046568586577,REPAYMENT,100.00,-819480.37\n"
    )
    mapping = {**MAPPING, "balance_col": 4}
    found = auth_client.post(
        "/api/imports/accounts/scan",
        files={"file": ("s.csv", body, "text/csv")},
        data={"file_format": "csv", "mapping": json.dumps(mapping)},
    ).json()
    (row,) = found
    assert row["row_count"] == 2
    assert row["first_date"] == "2025-06-01"
    assert row["last_date"] == "2025-06-02"
    # The balance on the most recent row: what tells a mortgage from an offset.
    assert row["latest_balance_cents"] == -81948037
