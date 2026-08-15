"""Resolve a period selector to a concrete date range, honouring the household's
financial year and pay-cycle basis (PRD R1/R17/§9).

Selectors are either relative (`this_month`, `last_30d`, `this_period`, `this_fy`)
or explicit (`fy:2024`, `q:2024-2`, `month:2025-03`, `all`, `custom` with dates).
Explicit ones are what the app's global period picker sends, so a chosen window
survives a reload and can be shared in a link.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from dateutil.relativedelta import relativedelta

from .. import models

# A financial year is four quarters, counted from the household's FY start month
# rather than from January.
QUARTERS_PER_YEAR = 4
MONTHS_PER_QUARTER = 3


def fy_bounds(household: models.Household, today: dt.date) -> tuple[dt.date, dt.date]:
    month, day = household.fy_start_month, household.fy_start_day
    start_year = today.year if (today.month, today.day) >= (month, day) else today.year - 1
    start = dt.date(start_year, month, day)
    end = start + relativedelta(years=1) - dt.timedelta(days=1)
    return start, end


def fy_bounds_for_year(household: models.Household, fy_start_year: int) -> tuple[dt.date, dt.date]:
    """Bounds of the financial year that begins in `fy_start_year`."""
    start = dt.date(fy_start_year, household.fy_start_month, household.fy_start_day)
    return start, start + relativedelta(years=1) - dt.timedelta(days=1)


def fy_label(start: dt.date, end: dt.date) -> str:
    """`FY2025–26` for a year spanning two calendar years; plain `2025` when the
    household runs its financial year over the calendar year."""
    if start.year == end.year:
        return str(start.year)
    return f"FY{start.year}–{end.year % 100:02d}"


def quarter_bounds(
    household: models.Household, fy_start_year: int, quarter: int
) -> tuple[dt.date, dt.date]:
    if not 1 <= quarter <= QUARTERS_PER_YEAR:
        raise ValueError(f"quarter must be 1-{QUARTERS_PER_YEAR}")
    fy_start, _ = fy_bounds_for_year(household, fy_start_year)
    start = fy_start + relativedelta(months=MONTHS_PER_QUARTER * (quarter - 1))
    return start, start + relativedelta(months=MONTHS_PER_QUARTER) - dt.timedelta(days=1)


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


def _explicit(
    household: models.Household, period: str, all_bounds: tuple[dt.date, dt.date] | None
) -> tuple[dt.date, dt.date, str] | None:
    """Resolve a selector that names an exact window. Returns None if `period` is not
    one of these forms; raises ValueError if it is but is malformed."""
    prefix, _, rest = period.partition(":")
    try:
        if prefix == "fy":
            s, e = fy_bounds_for_year(household, int(rest))
            return s, e, fy_label(s, e)
        if prefix == "q":
            year, _, quarter = rest.partition("-")
            s, e = quarter_bounds(household, int(year), int(quarter))
            fy_start, fy_end = fy_bounds_for_year(household, int(year))
            return s, e, f"Q{int(quarter)} {fy_label(fy_start, fy_end)}"
        if prefix == "month":
            year, _, month = rest.partition("-")
            s = dt.date(int(year), int(month), 1)
            return s, s + relativedelta(months=1) - dt.timedelta(days=1), s.strftime("%B %Y")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Unrecognised period '{period}'") from exc
    if period == "all":
        if all_bounds is None:
            raise ValueError("'all' needs the data range")
        return all_bounds[0], all_bounds[1], "All time"
    return None


def resolve_period(
    household: models.Household,
    period: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    today: dt.date | None = None,
    all_bounds: tuple[dt.date, dt.date] | None = None,
) -> tuple[dt.date, dt.date, str]:
    today = today or dt.date.today()

    explicit = _explicit(household, period, all_bounds)
    if explicit is not None:
        return explicit
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
    return s, e, fy_label(s, e)


@dataclass(frozen=True)
class ResolvedPeriod:
    """A selected window, plus the date that views which look forward or report a
    point in time should treat as "now"."""

    start: dt.date
    end: dt.date
    label: str
    as_at: dt.date
    is_current: bool


def resolve(
    household: models.Household,
    period: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
    today: dt.date | None = None,
    all_bounds: tuple[dt.date, dt.date] | None = None,
) -> ResolvedPeriod:
    today = today or dt.date.today()
    s, e, label = resolve_period(household, period, start, end, today, all_bounds)
    is_current = s <= today <= e
    # Forecasts, upcoming bills, goal progress and the net-worth position are all
    # "as of" a moment rather than over a span. Anchoring them to the end of a past
    # window (or the start of a future one) makes those views answer for the period
    # being looked at instead of silently reporting today.
    as_at = today if is_current else (e if e < today else s)
    return ResolvedPeriod(start=s, end=e, label=label, as_at=as_at, is_current=is_current)
