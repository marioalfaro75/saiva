"""Overview aggregations (PRD R17–R19). Transfers are excluded; split parents are
excluded in favour of their child rows to avoid double counting."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import (
    CategoryBreakdownItem,
    CategoryBreakdownOut,
    SummaryOut,
    TrendOut,
    TrendPoint,
)
from .periods import resolve_period


def _split_parent_ids(db: Session, household_id: str) -> set[str]:
    rows = db.execute(
        select(models.Transaction.split_parent_id).where(
            models.Transaction.household_id == household_id,
            models.Transaction.split_parent_id.is_not(None),
        )
    ).all()
    return {r[0] for r in rows if r[0]}


def _spendable_leaves(
    db: Session, household_id: str, start: dt.date, end: dt.date
) -> list[models.Transaction]:
    parent_ids = _split_parent_ids(db, household_id)
    txns = (
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household_id,
                models.Transaction.txn_date >= start,
                models.Transaction.txn_date <= end,
                models.Transaction.is_transfer.is_(False),
            )
        )
        .scalars()
        .all()
    )
    return [t for t in txns if t.id not in parent_ids]


def summary(
    db: Session,
    household: models.Household,
    period: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> SummaryOut:
    s, e, label = resolve_period(household, period, start, end)
    leaves = _spendable_leaves(db, household.id, s, e)
    income = sum(t.amount_cents for t in leaves if t.amount_cents > 0)
    expense = -sum(t.amount_cents for t in leaves if t.amount_cents < 0)
    net = income - expense
    savings_rate = round(net / income, 4) if income > 0 else 0.0
    return SummaryOut(
        period_label=label,
        start=s,
        end=e,
        income_cents=income,
        expense_cents=expense,
        net_cents=net,
        savings_rate=savings_rate,
        txn_count=len(leaves),
    )


def category_breakdown(
    db: Session,
    household: models.Household,
    period: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> CategoryBreakdownOut:
    s, e, _ = resolve_period(household, period, start, end)
    leaves = _spendable_leaves(db, household.id, s, e)
    categories = {
        c.id: c
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household.id)
        )
        .scalars()
        .all()
    }

    sums: dict[str | None, int] = defaultdict(int)
    for t in leaves:
        if t.amount_cents < 0:
            sums[t.category_id] += -t.amount_cents

    total = sum(sums.values())
    items: list[CategoryBreakdownItem] = []
    for category_id, amount in sorted(sums.items(), key=lambda kv: kv[1], reverse=True):
        category = categories.get(category_id) if category_id else None
        parent = categories.get(category.parent_id) if category and category.parent_id else None
        items.append(
            CategoryBreakdownItem(
                category_id=category_id,
                category_name=category.name if category else "Uncategorised",
                parent_name=parent.name if parent else None,
                kind=category.kind if category else "expense",
                amount_cents=amount,
                pct=round(amount / total, 4) if total else 0.0,
            )
        )
    return CategoryBreakdownOut(start=s, end=e, total_cents=total, items=items)


def trends(
    db: Session,
    household: models.Household,
    period: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> TrendOut:
    s, e, _ = resolve_period(household, period, start, end)
    leaves = _spendable_leaves(db, household.id, s, e)

    buckets: dict[dt.date, list[int]] = defaultdict(lambda: [0, 0])
    for t in leaves:
        key = t.txn_date.replace(day=1)
        if t.amount_cents > 0:
            buckets[key][0] += t.amount_cents
        else:
            buckets[key][1] += -t.amount_cents

    points: list[TrendPoint] = []
    cursor = s.replace(day=1)
    while cursor <= e:
        income, expense = buckets.get(cursor, [0, 0])
        points.append(
            TrendPoint(
                period_start=cursor,
                income_cents=income,
                expense_cents=expense,
                net_cents=income - expense,
            )
        )
        cursor = cursor + relativedelta(months=1)
    return TrendOut(interval="month", points=points)
