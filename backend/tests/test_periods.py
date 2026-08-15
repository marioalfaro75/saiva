"""The global period picker: resolving selectors against the household's own
financial-year settings, and applying the chosen window across the app."""

from __future__ import annotations

import datetime as dt

from conftest import create_account
from fastapi.testclient import TestClient

from app import models
from app.services import periods


def _household(fy_start_month: int = 7, fy_start_day: int = 1) -> models.Household:
    return models.Household(
        id="h", name="Test", fy_start_month=fy_start_month, fy_start_day=fy_start_day
    )


# ------------------------------------------------------------------ resolution


def test_named_financial_year_uses_household_settings() -> None:
    start, end, label = periods.resolve_period(_household(), "fy:2024")
    assert (start, end) == (dt.date(2024, 7, 1), dt.date(2025, 6, 30))
    assert label == "FY2024–25"


def test_calendar_year_household_labels_a_plain_year() -> None:
    """A January financial-year start is the calendar year, so "FY2025–26" would be
    wrong — it is simply 2025."""
    start, end, label = periods.resolve_period(_household(fy_start_month=1), "fy:2025")
    assert (start, end) == (dt.date(2025, 1, 1), dt.date(2025, 12, 31))
    assert label == "2025"


def test_quarters_run_from_the_financial_year_start() -> None:
    start, end, label = periods.resolve_period(_household(), "q:2024-1")
    assert (start, end) == (dt.date(2024, 7, 1), dt.date(2024, 9, 30))
    assert label == "Q1 FY2024–25"
    # Q3 of a July year is January to March.
    start, end, _ = periods.resolve_period(_household(), "q:2024-3")
    assert (start, end) == (dt.date(2025, 1, 1), dt.date(2025, 3, 31))


def test_quarters_follow_a_calendar_year_household() -> None:
    start, end, _ = periods.resolve_period(_household(fy_start_month=1), "q:2025-1")
    assert (start, end) == (dt.date(2025, 1, 1), dt.date(2025, 3, 31))


def test_month_selector() -> None:
    start, end, label = periods.resolve_period(_household(), "month:2025-02")
    assert (start, end) == (dt.date(2025, 2, 1), dt.date(2025, 2, 28))
    assert label == "February 2025"


def test_all_time_uses_the_data_range() -> None:
    bounds = (dt.date(2019, 3, 4), dt.date(2025, 8, 9))
    start, end, label = periods.resolve_period(_household(), "all", all_bounds=bounds)
    assert (start, end) == bounds
    assert label == "All time"


def test_relative_selectors_are_unchanged() -> None:
    today = dt.date(2025, 8, 15)
    start, end, _ = periods.resolve_period(_household(), "this_month", today=today)
    assert (start, end) == (dt.date(2025, 8, 1), dt.date(2025, 8, 31))
    start, end, label = periods.resolve_period(_household(), "this_fy", today=today)
    assert (start, end) == (dt.date(2025, 7, 1), dt.date(2026, 6, 30))
    assert label == "FY2025–26"


def test_malformed_selectors_are_rejected() -> None:
    for bad in ("fy:nope", "q:2024-9", "q:2024-x", "month:2025-13"):
        try:
            periods.resolve_period(_household(), bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} should not resolve")


# ------------------------------------------------- the "as at" date for forward views


def test_as_at_is_today_inside_the_current_window() -> None:
    today = dt.date(2025, 8, 15)
    window = periods.resolve(_household(), "fy:2025", today=today)
    assert window.is_current is True
    assert window.as_at == today


def test_as_at_is_the_end_of_a_past_window() -> None:
    """Forecasts and upcoming bills answer for the period being looked at, not today."""
    window = periods.resolve(_household(), "fy:2023", today=dt.date(2025, 8, 15))
    assert window.is_current is False
    assert window.as_at == dt.date(2024, 6, 30)


def test_as_at_is_the_start_of_a_future_window() -> None:
    window = periods.resolve(_household(), "fy:2030", today=dt.date(2025, 8, 15))
    assert window.is_current is False
    assert window.as_at == dt.date(2030, 7, 1)


# --------------------------------------------------------------------- the API


def test_options_lists_years_quarters_and_months(auth_client: TestClient) -> None:
    options = auth_client.get("/api/periods/options").json()
    assert options["default"].startswith("fy:")
    assert {o["value"] for o in options["relative"]} >= {"this_month", "last_30d"}
    year = options["financial_years"][0]
    assert len(year["quarters"]) == 4
    assert len(year["months"]) == 12
    assert year["quarters"][0]["value"].startswith("q:")
    assert year["months"][0]["value"].startswith("month:")


def test_resolve_endpoint_reports_the_window(auth_client: TestClient) -> None:
    resolved = auth_client.get("/api/periods/resolve", params={"period": "fy:2020"}).json()
    assert resolved["start"] == "2020-07-01"
    assert resolved["end"] == "2021-06-30"
    assert resolved["label"] == "FY2020–21"
    assert resolved["is_current"] is False


def test_resolve_endpoint_rejects_a_bad_selector(auth_client: TestClient) -> None:
    assert auth_client.get("/api/periods/resolve", params={"period": "fy:soon"}).status_code == 400


def test_period_filters_the_dashboard_and_transactions(auth_client: TestClient) -> None:
    account = create_account(auth_client)
    for date, amount in (("2023-09-01", -1000), ("2024-09-01", -2500)):
        auth_client.post(
            "/api/transactions",
            json={
                "account_id": account["id"], "txn_date": date,
                "amount_cents": amount, "description": "GROCERIES",
            },
        )

    older = auth_client.get("/api/transactions", params={"period": "fy:2023"}).json()
    assert older["total"] == 1
    assert older["items"][0]["txn_date"] == "2023-09-01"

    newer = auth_client.get("/api/transactions", params={"period": "fy:2024"}).json()
    assert newer["total"] == 1
    assert newer["items"][0]["txn_date"] == "2024-09-01"

    summary = auth_client.get("/api/dashboard/summary", params={"period": "fy:2023"}).json()
    assert summary["expense_cents"] == 1000

    everything = auth_client.get("/api/transactions", params={"period": "all"}).json()
    assert everything["total"] == 2


def test_transactions_without_a_period_are_unfiltered(auth_client: TestClient) -> None:
    """Existing callers that pass no period keep seeing everything."""
    account = create_account(auth_client)
    auth_client.post(
        "/api/transactions",
        json={
            "account_id": account["id"], "txn_date": "2019-01-01",
            "amount_cents": -500, "description": "OLD",
        },
    )
    assert auth_client.get("/api/transactions").json()["total"] == 1


def test_period_applies_to_the_other_views(auth_client: TestClient) -> None:
    """Every period-aware endpoint accepts the same selector."""
    for path in (
        "/api/insights", "/api/benchmarks", "/api/budgets", "/api/goals",
        "/api/recurring", "/api/recurring/upcoming", "/api/net-worth",
    ):
        resp = auth_client.get(path, params={"period": "fy:2023"})
        assert resp.status_code == 200, f"{path}: {resp.text}"
    forecast = auth_client.post("/api/forecast?period=fy:2023", json={"days": 30})
    assert forecast.status_code == 200, forecast.text


def test_bad_period_is_rejected_consistently(auth_client: TestClient) -> None:
    for path in ("/api/transactions", "/api/insights", "/api/dashboard/summary"):
        assert auth_client.get(path, params={"period": "fy:???"}).status_code == 400
