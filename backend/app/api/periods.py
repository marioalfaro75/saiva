"""The catalogue behind the app's global period picker (PRD R1/R17).

Everything is derived from the household's own financial-year settings, so a
household running July–June sees `FY2025–26` with quarters starting in July, and
one running the calendar year sees `2025` with quarters starting in January.
"""

from __future__ import annotations

import datetime as dt

from dateutil.relativedelta import relativedelta
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, get_household, resolved_period
from ..services import periods
from ..services.reports import available_years

router = APIRouter(prefix="/periods", tags=["periods"])

RELATIVE = [
    ("this_month", "This month"),
    ("last_month", "Last month"),
    ("last_30d", "Last 30 days"),
    ("last_90d", "Last 90 days"),
    ("this_period", "Pay period"),
]


def _quarters(household: models.Household, year: int) -> list[schemas.PeriodOption]:
    out = []
    for q in range(1, periods.QUARTERS_PER_YEAR + 1):
        start, end = periods.quarter_bounds(household, year, q)
        out.append(
            schemas.PeriodOption(
                value=f"q:{year}-{q}", label=f"Q{q} ({start:%b}–{end:%b %Y})"
            )
        )
    return out


def _months(household: models.Household, year: int) -> list[schemas.PeriodOption]:
    start, _ = periods.fy_bounds_for_year(household, year)
    return [
        schemas.PeriodOption(
            value=f"month:{(start + relativedelta(months=i)):%Y-%m}",
            label=f"{(start + relativedelta(months=i)):%B %Y}",
        )
        for i in range(12)
    ]


@router.get("/options", response_model=schemas.PeriodOptionsOut)
def options(
    user: models.User = Depends(get_current_user),
    household: models.Household = Depends(get_household),
    db: Session = Depends(get_db),
) -> schemas.PeriodOptionsOut:
    today = dt.date.today()
    current_start, _ = periods.fy_bounds(household, today)
    years = [
        schemas.PeriodFinancialYear(
            value=f"fy:{option.year}",
            label=periods.fy_label(option.start, option.end),
            start=option.start,
            end=option.end,
            quarters=_quarters(household, option.year),
            months=_months(household, option.year),
        )
        # available_years covers the span of the data, always including this year.
        for option in available_years(db, household, today)
    ]
    return schemas.PeriodOptionsOut(
        default=f"fy:{current_start.year}",
        relative=[schemas.PeriodOption(value=v, label=lbl) for v, lbl in RELATIVE],
        financial_years=years,
    )


@router.get("/resolve", response_model=schemas.ResolvedPeriodOut)
def resolve(
    window: periods.ResolvedPeriod = Depends(resolved_period),
) -> schemas.ResolvedPeriodOut:
    """What a selector actually covers — used to label the picker and to warn when
    the app is showing a period that has already ended."""
    return schemas.ResolvedPeriodOut(
        start=window.start, end=window.end, label=window.label, is_current=window.is_current
    )
