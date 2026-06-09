from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient

TODAY = dt.date.today().isoformat()


def test_fy_years_lists_current_year(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": TODAY,
            "amount_cents": -2500,
            "description": "SOME SHOP",
        },
    )
    years = auth_client.get("/api/reports/years").json()
    assert isinstance(years, list) and len(years) >= 1
    assert all({"year", "label", "start", "end"} <= set(y) for y in years)
    assert years[0]["label"].startswith("FY")


def test_fy_pdf_download(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": TODAY,
            "amount_cents": -9900,
            "description": "WOOLWORTHS METRO",
        },
    )
    resp = auth_client.get("/api/reports/fy")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:4] == b"%PDF"
    assert "attachment" in resp.headers.get("content-disposition", "")


def test_fy_pdf_empty_year_still_renders(auth_client: TestClient) -> None:
    create_account(auth_client)
    resp = auth_client.get("/api/reports/fy", params={"year": 2001})
    assert resp.status_code == 200
    assert resp.content[:4] == b"%PDF"
