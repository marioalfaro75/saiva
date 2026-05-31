from __future__ import annotations

from pathlib import Path

from conftest import create_account
from fastapi.testclient import TestClient

FIXTURES = Path(__file__).parent / "fixtures"


def test_ofx_commit(auth_client: TestClient) -> None:
    account = create_account(auth_client, "Savings", "savings")
    commit = auth_client.post(
        "/api/imports/commit",
        files={"file": ("sample.ofx", (FIXTURES / "sample.ofx").read_bytes(), "application/x-ofx")},
        data={"account_id": account["id"], "file_format": "ofx"},
    )
    assert commit.status_code == 200, commit.text
    assert commit.json()["added"] == 3

    netflix = auth_client.get("/api/transactions", params={"q": "netflix"}).json()["items"][0]
    assert netflix["category_name"] == "Streaming"
    assert netflix["amount_cents"] == -1999
