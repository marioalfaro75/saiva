"""Cashflow forecasting (PRD R27): project the household's total balance forward
from today. Recurring income lands as lumpy events on its due dates (paydays
matter), while spending is a per-category daily baseline from recent history.
Simple what-if adjustments scale a category's spend (e.g. "cut dining 20%").

Computed on the fly from existing data and the recurring detector — no new tables.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from . import recurring as recurring_service
from .dashboard import _spendable_leaves

BASELINE_DAYS = 90  # window for the per-category spend run-rate
DEFAULT_HORIZON = 90
POINT_STEP_DAYS = 7  # emit a chart point weekly (plus the final day)


@dataclass
class ForecastPoint:
    date: dt.date
    balance_cents: int


@dataclass
class Forecast:
    starting_balance_cents: int
    end_balance_cents: int
    low_balance_cents: int
    low_balance_date: dt.date
    horizon_days: int
    monthly_income_cents: int  # recurring income, normalised
    monthly_expense_cents: int  # baseline spend, normalised (after adjustments)
    points: list[ForecastPoint]


def _starting_balance(db: Session, household_id: str) -> int:
    opening = db.execute(
        select(func.coalesce(func.sum(models.Account.opening_balance_cents), 0)).where(
            models.Account.household_id == household_id
        )
    ).scalar_one()
    moved = db.execute(
        select(func.coalesce(func.sum(models.Transaction.amount_cents), 0)).where(
            models.Transaction.household_id == household_id,
            models.Transaction.split_parent_id.is_(None),
        )
    ).scalar_one()
    return int(opening) + int(moved)


def _daily_spend(
    db: Session, household_id: str, today: dt.date, adjustments: dict[str | None, float]
) -> float:
    """Average daily spend over the baseline window, applying per-category what-if
    multipliers (pct < 0 cuts spend, pct > 0 increases it)."""
    start = today - dt.timedelta(days=BASELINE_DAYS)
    total = 0.0
    for t in _spendable_leaves(db, household_id, start, today):
        if t.amount_cents < 0:
            factor = 1.0 + adjustments.get(t.category_id, 0.0) / 100.0
            total += (-t.amount_cents) * max(factor, 0.0)
    return total / BASELINE_DAYS


def forecast(
    db: Session,
    household_id: str,
    days: int = DEFAULT_HORIZON,
    adjustments: dict[str | None, float] | None = None,
    today: dt.date | None = None,
) -> Forecast:
    today = today or dt.date.today()
    adjustments = adjustments or {}

    start_balance = _starting_balance(db, household_id)
    daily_spend = _daily_spend(db, household_id, today, adjustments)

    series = recurring_service.detect(db, household_id, today=today)
    horizon = today + dt.timedelta(days=days)

    # Recurring income as dated events (paydays step the balance up).
    income_by_date: dict[dt.date, int] = {}
    for s in series:
        if s.direction != "income" or not s.active:
            continue
        due = s.next_due
        while due <= horizon:
            income_by_date[due] = income_by_date.get(due, 0) + s.typical_amount_cents
            due += dt.timedelta(days=s.interval_days)

    monthly_income = sum(
        s.monthly_amount_cents for s in series if s.direction == "income" and s.active
    )
    monthly_expense = int(round(daily_spend * recurring_service.DAYS_PER_MONTH))

    balance = float(start_balance)
    low_balance = balance
    low_date = today
    points = [ForecastPoint(today, int(round(balance)))]
    for i in range(1, days + 1):
        day = today + dt.timedelta(days=i)
        balance += income_by_date.get(day, 0)
        balance -= daily_spend
        if balance < low_balance:
            low_balance = balance
            low_date = day
        if i % POINT_STEP_DAYS == 0 or i == days:
            points.append(ForecastPoint(day, int(round(balance))))

    return Forecast(
        starting_balance_cents=start_balance,
        end_balance_cents=int(round(balance)),
        low_balance_cents=int(round(low_balance)),
        low_balance_date=low_date,
        horizon_days=days,
        monthly_income_cents=monthly_income,
        monthly_expense_cents=monthly_expense,
        points=points,
    )
