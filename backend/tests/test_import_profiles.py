"""Remembering how a shape of statement is read.

Mapping eight columns once is fine; doing it every month is not. A profile is keyed
on a fingerprint of the header row, so the next export from the same bank opens
already mapped — and a bank that adds or renames a column no longer matches, which
is the point: it gets seen rather than absorbed.
"""

from __future__ import annotations

import json

from conftest import create_account
from fastapi.testclient import TestClient

WESTPAC_HEADER = "Bank Account,Date,Narrative,Debit Amount,Credit Amount,Balance,Serial\n"
ROW = "046568586577,01/06/2025,WOOLWORTHS METRO,30.00,,-819480.37,\n"

MAPPING = {
    "has_header": True,
    "date_col": 1,
    "description_col": 2,
    "amount_mode": "debit_credit",
    "debit_col": 3,
    "credit_col": 4,
    "balance_col": 5,
    "account_col": 0,
    "delimiter": ",",
}


def _file(header: str = WESTPAC_HEADER, row: str = ROW) -> bytes:
    return (header + row).encode()


def _post(client: TestClient, path: str, content: bytes, **data: str):
    resp = client.post(
        f"/api/imports/{path}",
        files={"file": ("westpac.csv", content, "text/csv")},
        data={"file_format": "csv", **data},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _commit(client: TestClient, account_id: str, content: bytes = b"") -> dict:
    return _post(
        client,
        "commit",
        content or _file(),
        mapping=json.dumps(MAPPING),
        assignments=json.dumps([{"value": "046568586577", "account_id": account_id}]),
        account_id="",
    )


def test_the_next_file_of_the_same_shape_opens_already_mapped(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)
    _commit(auth_client, account["id"])

    sniffed = _post(auth_client, "sniff", _file())
    assert sniffed["profile"] is not None
    saved = sniffed["profile"]["mapping"]
    assert saved["date_col"] == 1
    assert saved["description_col"] == 2
    assert saved["account_col"] == 0
    assert (saved["debit_col"], saved["credit_col"]) == (3, 4)


def test_a_first_import_has_nothing_to_remember(auth_client: TestClient) -> None:
    assert _post(auth_client, "sniff", _file())["profile"] is None


def test_cosmetic_header_changes_keep_the_profile(auth_client: TestClient) -> None:
    """Case and spacing are not a different file."""
    account = create_account(auth_client)
    _commit(auth_client, account["id"])
    respaced = "BANK ACCOUNT, Date ,Narrative,Debit  Amount,Credit Amount,Balance,Serial\n"
    assert _post(auth_client, "sniff", _file(respaced))["profile"] is not None


def test_a_bank_adding_a_column_is_noticed(auth_client: TestClient) -> None:
    """The mapping would still be readable, but the new column has never been ruled on
    — so the profile stops matching and the step is shown from scratch."""
    account = create_account(auth_client)
    _commit(auth_client, account["id"])
    wider = WESTPAC_HEADER.rstrip("\n") + ",Categories\n"
    assert _post(auth_client, "sniff", _file(wider, ROW.rstrip("\n") + ",DEP\n"))["profile"] is None


def test_a_value_bound_to_an_account_is_not_duplicated_into_the_profile(
    auth_client: TestClient,
) -> None:
    """A full account number lives on the account, where it survives the profile being
    edited. Only what cannot be stored there is kept in the profile."""
    account = create_account(auth_client)
    _commit(auth_client, account["id"])
    assert _post(auth_client, "sniff", _file())["profile"]["account_map"] == {}


def test_a_fragment_that_cannot_bind_is_kept_in_the_profile(
    auth_client: TestClient,
) -> None:
    account = create_account(auth_client)
    body = (WESTPAC_HEADER + "5263,01/06/2025,UBER TRIP,24.50,,0,\n").encode()
    _post(
        auth_client,
        "commit",
        body,
        mapping=json.dumps(MAPPING),
        assignments=json.dumps([{"value": "5263", "account_id": account["id"]}]),
        account_id="",
    )
    profile = _post(auth_client, "sniff", body)["profile"]
    assert profile["account_map"] == {"5263": account["id"]}
