"""Recurring & subscription detection (PRD R16) and the upcoming-bills view (R26).

Computed on the fly from existing transactions — no new tables. We group the
household's spendable transactions by normalised merchant, collapse same-day
charges into a single occurrence, then look for a stable cadence (weekly … yearly)
with a reasonably stable amount. Detected series feed the bills view and, later,
cashflow forecasting (R27).
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .dashboard import _spendable_leaves

LOOKBACK_DAYS = 550  # ~18 months of history to establish a cadence
MIN_OCCURRENCES = 3

# (label, canonical interval days, min gap, max gap)
_CADENCES: list[tuple[str, int, int, int]] = [
    ("weekly", 7, 5, 9),
    ("fortnightly", 14, 12, 17),
    ("monthly", 30, 26, 35),
    ("quarterly", 91, 84, 100),
    ("yearly", 365, 350, 380),
]
GAP_TOLERANCE = 0.35  # a gap "matches" the cadence if within ±35% of the median
MIN_REGULAR_RATIO = 0.6  # at least this fraction of gaps must match
RECURRING_AMOUNT_CV = 0.35  # amounts must be at least this stable to count at all
SUBSCRIPTION_AMOUNT_CV = 0.15  # tighter stability ⇒ flagged as a "subscription"
DAYS_PER_MONTH = 30.44


@dataclass
class RecurringSeries:
    merchant: str
    category_id: str | None
    category_name: str | None
    direction: str  # "expense" | "income"
    cadence: str
    interval_days: int
    typical_amount_cents: int  # median magnitude of an occurrence
    monthly_amount_cents: int  # normalised to a month
    occurrences: int
    first_date: dt.date
    last_date: dt.date
    next_due: dt.date
    active: bool
    is_subscription: bool


@dataclass
class UpcomingBill:
    due_date: dt.date
    merchant: str
    amount_cents: int
    category_id: str | None
    category_name: str | None
    cadence: str


def _classify(median_gap: float) -> tuple[str, int] | None:
    for label, canonical, lo, hi in _CADENCES:
        if lo <= median_gap <= hi:
            return label, canonical
    return None


def _cv(amounts: list[int]) -> float:
    """Coefficient of variation of magnitudes (0 = perfectly stable)."""
    mags = [abs(a) for a in amounts]
    mean = statistics.fmean(mags)
    if mean == 0:
        return 1.0
    if len(mags) < 2:
        return 0.0
    return statistics.pstdev(mags) / mean


def _dominant_category(leaves: list[models.Transaction]) -> dict[str, str]:
    """Most frequently used category id per merchant, for labelling a series."""
    counts: dict[str, dict[str, int]] = {}
    for t in leaves:
        if t.merchant and t.category_id:
            per = counts.setdefault(t.merchant, {})
            per[t.category_id] = per.get(t.category_id, 0) + 1
    return {m: max(per, key=lambda c: per[c]) for m, per in counts.items()}


def detect(
    db: Session, household_id: str, today: dt.date | None = None
) -> list[RecurringSeries]:
    today = today or dt.date.today()
    start = today - dt.timedelta(days=LOOKBACK_DAYS)
    leaves = _spendable_leaves(db, household_id, start, today)

    # merchant -> {date -> summed amount}; collapses splits and same-day charges.
    by_merchant: dict[str, dict[dt.date, int]] = {}
    for t in leaves:
        if not t.merchant:
            continue
        per_day = by_merchant.setdefault(t.merchant, {})
        per_day[t.txn_date] = per_day.get(t.txn_date, 0) + t.amount_cents

    cat_names = {
        c.id: c.name
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household_id)
        ).scalars()
    }
    cat_by_merchant = _dominant_category(leaves)

    series: list[RecurringSeries] = []
    for merchant, per_day in by_merchant.items():
        dates = sorted(per_day)
        if len(dates) < MIN_OCCURRENCES:
            continue
        amounts = [per_day[d] for d in dates]
        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        median_gap = float(statistics.median(gaps))
        classified = _classify(median_gap)
        if classified is None:
            continue
        cadence, interval = classified
        regular = sum(1 for g in gaps if abs(g - median_gap) <= GAP_TOLERANCE * median_gap)
        if regular / len(gaps) < MIN_REGULAR_RATIO:
            continue
        amount_cv = _cv(amounts)
        if amount_cv > RECURRING_AMOUNT_CV:
            continue

        typical = int(round(statistics.median(abs(a) for a in amounts)))
        direction = "income" if statistics.median(amounts) > 0 else "expense"
        last_date = dates[-1]
        next_due = last_date + dt.timedelta(days=interval)
        while next_due < today:
            next_due += dt.timedelta(days=interval)
        category_id = cat_by_merchant.get(merchant)
        series.append(
            RecurringSeries(
                merchant=merchant,
                category_id=category_id,
                category_name=cat_names.get(category_id) if category_id else None,
                direction=direction,
                cadence=cadence,
                interval_days=interval,
                typical_amount_cents=typical,
                monthly_amount_cents=int(round(typical * DAYS_PER_MONTH / interval)),
                occurrences=len(dates),
                first_date=dates[0],
                last_date=last_date,
                next_due=next_due,
                active=last_date >= today - dt.timedelta(days=int(interval * 1.5)),
                is_subscription=direction == "expense" and amount_cv <= SUBSCRIPTION_AMOUNT_CV,
            )
        )
    series.sort(key=lambda s: s.monthly_amount_cents, reverse=True)
    return series


def upcoming_bills(
    db: Session, household_id: str, days: int = 60, today: dt.date | None = None
) -> list[UpcomingBill]:
    today = today or dt.date.today()
    horizon = today + dt.timedelta(days=days)
    bills: list[UpcomingBill] = []
    for s in detect(db, household_id, today=today):
        if s.direction != "expense" or not s.active:
            continue
        due = s.next_due
        while due <= horizon:
            bills.append(
                UpcomingBill(
                    due_date=due,
                    merchant=s.merchant,
                    amount_cents=s.typical_amount_cents,
                    category_id=s.category_id,
                    category_name=s.category_name,
                    cadence=s.cadence,
                )
            )
            due += dt.timedelta(days=s.interval_days)
    bills.sort(key=lambda b: b.due_date)
    return bills
