"""ABS spending benchmarks (PRD Phase 2): compare the household's typical weekly
spend per broad category against indicative Australian averages.

Reference figures are derived from the ABS Household Expenditure Survey 2015–16
(average weekly household expenditure on goods and services), mapped onto this
app's top-level categories and CPI-uplifted to the present. They are indicative,
not a live feed, and are scaled to household size with a simple modified-OECD
equivalence factor. The mapping and uplift are deliberately rough — this is a
guide, not a target. Transfers and split parents are excluded via the
dashboard's spendable-leaf logic so figures line up with the overview."""

from __future__ import annotations

import datetime as dt

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import BenchmarkItem, BenchmarkOut
from .dashboard import _spendable_leaves

# CPI uplift from 2015–16 to the present (ABS CPI index ~108 -> ~140). Indicative.
CPI_UPLIFT = 1.30

# ABS average household ≈ 2.6 persons; modified-OECD factor for ~1.9 adults + 0.6 kids.
AVERAGE_HOUSEHOLD_FACTOR = 1.6

WEEKS_PER_MONTH = 52 / 12
MONTHS_LOOKBACK = 3

# Indicative ABS HES 2015–16 average weekly spend per household, mapped to our
# top-level categories, in cents *before* CPI uplift and household scaling.
_BASE_WEEKLY_CENTS: dict[str, int] = {
    "Housing": 34000,
    "Transport": 20700,
    "Groceries": 16000,
    "Entertainment & Recreation": 12000,
    "Food & Drink (out)": 10000,
    "Shopping": 10000,
    "Utilities": 9000,
    "Health": 8200,
    "Travel & Holidays": 6500,
    "Children & Education": 6000,
    "Insurance & Finance": 5500,
    "Personal care": 2800,
    "Subscriptions & Memberships": 2000,
    "Taxes & Government": 1500,
    "Pets": 1500,
    "Donations & Gifts": 1200,
}


def _equivalence_factor(adults: int, children: int) -> float:
    adults = max(adults, 1)
    return 1.0 + 0.5 * (adults - 1) + 0.3 * max(children, 0)


def _root_name(category: models.Category, by_id: dict[str, models.Category]) -> str:
    seen: set[str] = set()
    current = category
    while current.parent_id and current.parent_id in by_id and current.id not in seen:
        seen.add(current.id)
        current = by_id[current.parent_id]
    return current.name


def _your_weekly_by_category(db: Session, household_id: str, today: dt.date) -> dict[str, int]:
    first_this = today.replace(day=1)
    start = first_this - relativedelta(months=MONTHS_LOOKBACK)
    end = first_this - dt.timedelta(days=1)  # end of the last full month
    leaves = _spendable_leaves(db, household_id, start, end)
    by_id = {
        c.id: c
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household_id)
        )
        .scalars()
        .all()
    }
    months: set[tuple[int, int]] = set()
    totals: dict[str, int] = {}
    for t in leaves:
        if t.amount_cents >= 0:
            continue
        months.add((t.txn_date.year, t.txn_date.month))
        category = by_id.get(t.category_id) if t.category_id else None
        if category is None:
            continue
        root = _root_name(category, by_id)
        totals[root] = totals.get(root, 0) + (-t.amount_cents)
    n = len(months) or 1  # average over the months that actually have data
    return {name: round(total / n / WEEKS_PER_MONTH) for name, total in totals.items()}


def benchmark(
    db: Session, household: models.Household, today: dt.date | None = None
) -> BenchmarkOut:
    today = today or dt.date.today()
    yours = _your_weekly_by_category(db, household.id, today)
    scale = _equivalence_factor(household.adults, household.children) / AVERAGE_HOUSEHOLD_FACTOR

    items: list[BenchmarkItem] = []
    your_total = 0
    typical_total = 0
    for name, base in _BASE_WEEKLY_CENTS.items():
        typical = round(base * CPI_UPLIFT * scale)
        your = yours.get(name, 0)
        your_total += your
        typical_total += typical
        items.append(
            BenchmarkItem(
                category=name,
                your_weekly_cents=your,
                typical_weekly_cents=typical,
                diff_cents=your - typical,
                pct_of_typical=round(your / typical, 4) if typical > 0 else 0.0,
            )
        )
    items.sort(key=lambda i: i.your_weekly_cents, reverse=True)
    return BenchmarkOut(
        basis="Your average weekly spend over the last 3 months",
        adults=max(household.adults, 1),
        children=max(household.children, 0),
        note=(
            "Indicative only. Based on the ABS Household Expenditure Survey 2015–16, "
            "CPI-adjusted and scaled to your household size — a rough guide, not a target."
        ),
        your_total_weekly_cents=your_total,
        typical_total_weekly_cents=typical_total,
        items=items,
    )
