"""Budget tracking (PRD R24): flexible per-category spending limits over a
recurring period, with progress, a projected end-of-period figure, and an
over/under status. Spend is rolled up over the category's subtree and reuses the
overview's spendable-leaf logic, so the numbers match the dashboard exactly."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import BudgetOut
from .dashboard import _spendable_leaves
from .periods import fy_bounds

WARN_RATIO = 0.8  # flag a budget once this much of the limit is spent or projected

_STATUS_ORDER = {"over": 0, "warning": 1, "ok": 2}


def budget_window(
    household: models.Household, period: str, today: dt.date
) -> tuple[dt.date, dt.date, str]:
    """Resolve a budget's recurring period to the current concrete date range."""
    if period == "annual":
        s, e = fy_bounds(household, today)
        return s, e, f"FY{e.year}"
    if period == "fortnightly":
        length = 14
        anchor = household.pay_cycle_anchor or today.replace(day=1)
        if today < anchor:
            anchor -= dt.timedelta(days=length * ((anchor - today).days // length + 1))
        cycles = (today - anchor).days // length
        s = anchor + dt.timedelta(days=cycles * length)
        e = s + dt.timedelta(days=length - 1)
        return s, e, f"Fortnight of {s.strftime('%d %b')}"
    # monthly (default)
    s = today.replace(day=1)
    e = s + relativedelta(months=1) - dt.timedelta(days=1)
    return s, e, s.strftime("%B %Y")


def _category_index(
    db: Session, household_id: str
) -> tuple[dict[str, models.Category], dict[str, list[str]]]:
    categories = {
        c.id: c
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household_id)
        )
        .scalars()
        .all()
    }
    children: dict[str, list[str]] = defaultdict(list)
    for c in categories.values():
        if c.parent_id:
            children[c.parent_id].append(c.id)
    return categories, children


def _subtree_ids(children: dict[str, list[str]], root_id: str) -> set[str]:
    """A category id plus all of its descendants (so a parent budget captures children)."""
    ids = {root_id}
    stack = [root_id]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in ids:
                ids.add(child)
                stack.append(child)
    return ids


def _build_out(
    budget: models.Budget,
    categories: dict[str, models.Category],
    children: dict[str, list[str]],
    leaves: list[models.Transaction],
    window: tuple[dt.date, dt.date, str],
    today: dt.date,
) -> BudgetOut:
    s, e, label = window
    subtree = _subtree_ids(children, budget.category_id)
    actual = sum(
        -t.amount_cents for t in leaves if t.amount_cents < 0 and t.category_id in subtree
    )
    limit = budget.limit_cents
    pct = round(actual / limit, 4) if limit > 0 else 0.0

    total_days = (e - s).days + 1
    elapsed_days = (min(today, e) - s).days + 1 if today >= s else 0
    projected = (
        round(actual / elapsed_days * total_days) if 0 < elapsed_days < total_days else actual
    )

    if actual >= limit:
        status = "over"
    elif pct >= WARN_RATIO or projected > limit:
        status = "warning"
    else:
        status = "ok"

    category = categories.get(budget.category_id)
    parent = categories.get(category.parent_id) if category and category.parent_id else None
    return BudgetOut(
        id=budget.id,
        category_id=budget.category_id,
        category_name=category.name if category else "—",
        parent_name=parent.name if parent else None,
        period=budget.period,
        period_label=label,
        period_start=s,
        period_end=e,
        limit_cents=limit,
        actual_cents=actual,
        remaining_cents=limit - actual,
        pct_used=pct,
        projected_cents=projected,
        status=status,
    )


def list_budgets(
    db: Session, household: models.Household, today: dt.date | None = None
) -> list[BudgetOut]:
    today = today or dt.date.today()
    budgets = (
        db.execute(select(models.Budget).where(models.Budget.household_id == household.id))
        .scalars()
        .all()
    )
    categories, children = _category_index(db, household.id)
    leaves_cache: dict[tuple[dt.date, dt.date], list[models.Transaction]] = {}
    out: list[BudgetOut] = []
    for budget in budgets:
        window = budget_window(household, budget.period, today)
        key = (window[0], window[1])
        leaves = leaves_cache.get(key)
        if leaves is None:
            leaves = _spendable_leaves(db, household.id, window[0], window[1])
            leaves_cache[key] = leaves
        out.append(_build_out(budget, categories, children, leaves, window, today))
    out.sort(key=lambda b: (_STATUS_ORDER.get(b.status, 3), -b.pct_used))
    return out


def compute_budget(
    db: Session,
    household: models.Household,
    budget: models.Budget,
    today: dt.date | None = None,
) -> BudgetOut:
    today = today or dt.date.today()
    categories, children = _category_index(db, household.id)
    window = budget_window(household, budget.period, today)
    leaves = _spendable_leaves(db, household.id, window[0], window[1])
    return _build_out(budget, categories, children, leaves, window, today)
