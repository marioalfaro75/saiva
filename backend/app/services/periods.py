"""Resolve a period selector to a concrete date range, honouring the household's
financial year and pay-cycle basis (PRD R1/R17/§9)."""

from __future__ import annotations

import datetime as dt

from dateutil.relativedelta import relativedelta

from .. import models


def fy_bounds(household: models.Household, today: dt.date) -> tuple[dt.date, dt.date]:
    month, day = household.fy_start_month, household.fy_start_day
    start_year = today.year if (today.month, today.day) >= (month, day) else today.year - 1
    start = dt.date(start_year, month, day)
    end = start + relativedelta(years=1) - dt.timedelta(days=1)
    return start, end


def current_pay_period(household: models.Household, today: dt.date) -> tuple[dt.date, dt.date, str]:
    basis = household.period_basis
    if basis in ("calendar", "monthly"):
        start = today.replace(day=1)
        end = start + relativedelta(months=1) - dt.timedelta(days=1)
        return start, end, start.strftime("%B %Y")

    length = 7 if basis == "weekly" else 14
    anchor = household.pay_cycle_anchor or dt.date(today.year, 1, 1)
    if today < anchor:
        anchor = anchor - dt.timedelta(days=length * ((anchor - today).days // length + 1))
    cycles = (today - anchor).days // length
    start = anchor + dt.timedelta(days=cycles * length)
    end = start + dt.timedelta(days=length - 1)
    label = f"{basis.capitalize()} from {start.isoformat()}"
    return start, end, label


def resolve_period(
    household: models.Household,
    period: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    today: dt.date | None = None,
) -> tuple[dt.date, dt.date, str]:
    today = today or dt.date.today()

    if period == "custom" and start and end:
        return start, end, "Custom range"
    if period == "last_30d":
        return today - dt.timedelta(days=29), today, "Last 30 days"
    if period == "last_90d":
        return today - dt.timedelta(days=89), today, "Last 90 days"
    if period == "this_month":
        s = today.replace(day=1)
        return s, s + relativedelta(months=1) - dt.timedelta(days=1), s.strftime("%B %Y")
    if period == "last_month":
        s = today.replace(day=1) - relativedelta(months=1)
        return s, today.replace(day=1) - dt.timedelta(days=1), s.strftime("%B %Y")
    if period == "this_period":
        return current_pay_period(household, today)
    # Default: this financial year.
    s, e = fy_bounds(household, today)
    return s, e, f"FY{e.year}"
