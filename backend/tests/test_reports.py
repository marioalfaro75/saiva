from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient


def _today() -> str:
    """Today, evaluated per call rather than captured when this module is imported.

    The module-level constant this replaces was read during pytest collection, minutes
    before the assertions that depended on it, so a suite run crossing UTC midnight
    compared one day's expectations against the next day's behaviour. Every use here is
    an independent write of a txn_date, so fresh evaluation is the whole fix; where a
    test needs one date to stay fixed across seeding AND asserting, it takes the `today`
    fixture from conftest instead.
    """
    return dt.date.today().isoformat()


def test_fy_years_lists_current_year(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"],
            "txn_date": _today(),
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
            "txn_date": _today(),
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
