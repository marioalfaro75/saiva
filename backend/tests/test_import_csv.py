from __future__ import annotations

import json
from pathlib import Path

from conftest import create_account
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"


def _csv_bytes() -> bytes:
    return (FIXTURES / "cba_sample.csv").read_bytes()


def test_sniff_detects_columns(auth_client: TestClient) -> None:
    resp = auth_client.post(
        "/api/imports/sniff", files={"file": ("cba.csv", _csv_bytes(), "text/csv")}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["has_header"] is True
    assert "Amount" in data["columns"]
    assert data["suggested_mapping"]["amount_mode"] == "single"


def test_preview_commit_dedup_and_categorisation(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    mapping = auth_client.post(
        "/api/imports/sniff", files={"file": ("cba.csv", _csv_bytes(), "text/csv")}
    ).json()["suggested_mapping"]
    form = {"account_id": account["id"], "file_format": "csv", "mapping": json.dumps(mapping)}

    preview = auth_client.post(
        "/api/imports/preview",
        files={"file": ("cba.csv", _csv_bytes(), "text/csv")},
        data=form,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["total_rows"] == 6
    assert preview.json()["duplicate_count"] == 0

    commit = auth_client.post(
        "/api/imports/commit",
        files={"file": ("cba.csv", _csv_bytes(), "text/csv")},
        data=form,
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["added"] == 6

    # Re-importing the same file adds nothing (dedup, PRD R6).
    recommit = auth_client.post(
        "/api/imports/commit",
        files={"file": ("cba.csv", _csv_bytes(), "text/csv")},
        data=form,
    )
    assert recommit.json() == {
        "batch_id": recommit.json()["batch_id"],
        "added": 0,
        "skipped": 6,
        "transfers_linked": recommit.json()["transfers_linked"],
    }

    # Starter rules categorised Woolworths as Supermarkets (PRD R11).
    txns = auth_client.get("/api/transactions", params={"q": "woolworths"}).json()
    assert txns["items"][0]["category_name"] == "Supermarkets"

    # Salary credit is positive income.
    salary = auth_client.get("/api/transactions", params={"q": "salary"}).json()["items"][0]
    assert salary["amount_cents"] == 380000
