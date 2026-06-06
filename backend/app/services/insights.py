"""Insights feed (PRD Phase 2): a ranked set of plain-English, rule-based cards
generated on the fly from the household's existing data — no new tables. Each
card explains what changed, why it matters, and links to the evidence.

Spending comparisons use whole calendar months (the last three complete months)
so partial-month noise never skews a "mover". Transfers and split parents are
excluded via the dashboard's spendable-leaf logic, so figures match the overview."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import Insight, InsightsOut
from .budgets import list_budgets
from .dashboard import _spendable_leaves
from .goals import list_goals

_SEVERITY_RANK = {"alert": 0, "warn": 1, "info": 2}

MOVER_MIN_CENTS = 5000  # ignore category swings smaller than $50...
MOVER_MIN_RATIO = 0.25  # ...unless they are at least a 25% change
MOVER_WARN_CENTS = 20000  # an increase this large is a warning, not just info
MAX_MOVERS = 5
CREEP_MIN_CENTS = 4000  # total rise across three months to count as "creeping"
FEE_MIN_CENTS = 100
FEE_WARN_CENTS = 2000
DUP_MIN_CENTS = 1000  # ignore duplicate charges under $10
MAX_INSIGHTS = 15

FEE_KEYWORDS = (
    "fee", "interest charge", "overdrawn", "overdraw", "dishonour", "dishonor",
    "late payment", "annual fee", "account keeping", "overlimit",
)


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _month_bounds(today: dt.date) -> list[tuple[dt.date, dt.date]]:
    """(start, end) for the last three complete calendar months, oldest first."""
    first_this = today.replace(day=1)
    bounds: list[tuple[dt.date, dt.date]] = []
    for n in range(3, 0, -1):
        start = first_this - relativedelta(months=n)
        end = start + relativedelta(months=1) - dt.timedelta(days=1)
        bounds.append((start, end))
    return bounds


def _spend_by_category(leaves: list[models.Transaction]) -> dict[str | None, int]:
    sums: dict[str | None, int] = defaultdict(int)
    for t in leaves:
        if t.amount_cents < 0:
            sums[t.category_id] += -t.amount_cents
    return sums


def _category_names(db: Session, household_id: str) -> dict[str, str]:
    return {
        c.id: c.name
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household_id)
        )
        .scalars()
        .all()
    }


def _txn_link(category_id: str | None) -> str:
    return f"/transactions?category_id={category_id}" if category_id else "/transactions"


def _looks_like_fee(t: models.Transaction) -> bool:
    text = f"{t.raw_description} {t.merchant or ''}".lower()
    return any(keyword in text for keyword in FEE_KEYWORDS)


def _mover(cid: str | None, name: str, prev: int, cur: int) -> Insight:
    delta = cur - prev
    up = delta > 0
    pct = round(abs(delta) / prev * 100) if prev else 100
    severity = "warn" if up and abs(delta) >= MOVER_WARN_CENTS else "info"
    body = (
        f"{name} was {_money(cur)} last month, vs {_money(prev)} the month before "
        f"({'+' if up else '-'}{_money(abs(delta))})."
    )
    return Insight(
        key=f"mover:{cid}",
        type="top_mover",
        severity=severity,
        title=f"{name} spending {'rose' if up else 'fell'} {pct}%",
        body=body,
        action="Review these transactions" if up else None,
        amount_cents=delta,
        link=_txn_link(cid),
    )


def _movers(
    spend_prev: dict[str | None, int],
    spend_cur: dict[str | None, int],
    names: dict[str, str],
    skip: set[str],
) -> list[Insight]:
    out: list[Insight] = []
    for cid in set(spend_cur) | set(spend_prev):
        if cid in skip:
            continue
        prev, cur = spend_prev.get(cid, 0), spend_cur.get(cid, 0)
        delta = cur - prev
        if abs(delta) < MOVER_MIN_CENTS:
            continue
        if prev and abs(delta) / prev < MOVER_MIN_RATIO:
            continue
        name = names.get(cid, "Uncategorised") if cid else "Uncategorised"
        out.append(_mover(cid, name, prev, cur))
    out.sort(key=lambda i: -abs(i.amount_cents or 0))
    return out[:MAX_MOVERS]


def _creeping(
    spend_a: dict[str | None, int],
    spend_b: dict[str | None, int],
    spend_c: dict[str | None, int],
    names: dict[str, str],
) -> list[Insight]:
    out: list[Insight] = []
    for cid in spend_c:
        if cid is None:
            continue
        va, vb, vc = spend_a.get(cid, 0), spend_b.get(cid, 0), spend_c.get(cid, 0)
        if va > 0 and va < vb < vc and (vc - va) >= CREEP_MIN_CENTS:
            name = names.get(cid, "Uncategorised")
            out.append(
                Insight(
                    key=f"creep:{cid}",
                    type="creeping",
                    severity="warn",
                    title=f"{name} keeps creeping up",
                    body=f"Three months running: {_money(va)} → {_money(vb)} → {_money(vc)}.",
                    action="Set a budget",
                    amount_cents=vc - va,
                    link="/budgets",
                )
            )
    return out


def _budget_alerts(db: Session, household: models.Household, today: dt.date) -> list[Insight]:
    out: list[Insight] = []
    for b in list_budgets(db, household, today):
        if b.status == "over":
            over = b.actual_cents - b.limit_cents
            out.append(
                Insight(
                    key=f"budget:{b.id}",
                    type="budget_alert",
                    severity="alert",
                    title=f"Over budget: {b.category_name}",
                    body=(
                        f"You've spent {_money(b.actual_cents)} of your "
                        f"{_money(b.limit_cents)} budget for {b.category_name} — "
                        f"{_money(over)} over."
                    ),
                    action="Review spending",
                    amount_cents=b.actual_cents,
                    link=_txn_link(b.category_id),
                )
            )
        elif b.status == "warning":
            out.append(
                Insight(
                    key=f"budget:{b.id}",
                    type="budget_alert",
                    severity="warn",
                    title=f"Approaching {b.category_name} budget",
                    body=(
                        f"{_money(b.actual_cents)} of {_money(b.limit_cents)} "
                        f"({round(b.pct_used * 100)}%) this period; "
                        f"projected {_money(b.projected_cents)}."
                    ),
                    action="Check spending",
                    amount_cents=b.actual_cents,
                    link=_txn_link(b.category_id),
                )
            )
    return out


def _fees(leaves: list[models.Transaction]) -> list[Insight]:
    fee_txns = [t for t in leaves if t.amount_cents < 0 and _looks_like_fee(t)]
    total = -sum(t.amount_cents for t in fee_txns)
    if not fee_txns or total < FEE_MIN_CENTS:
        return []
    example = (fee_txns[0].merchant or fee_txns[0].raw_description)[:40]
    return [
        Insight(
            key="fees",
            type="fees",
            severity="warn" if total >= FEE_WARN_CENTS else "info",
            title=f"You paid {_money(total)} in fees & interest",
            body=f"{len(fee_txns)} charge(s) last month, e.g. “{example}”.",
            action="See if these are avoidable",
            amount_cents=total,
            link="/transactions?q=fee",
        )
    ]


def _duplicates(leaves: list[models.Transaction]) -> list[Insight]:
    groups: dict[tuple[str, int], list[models.Transaction]] = defaultdict(list)
    for t in leaves:
        if t.amount_cents < 0:
            merchant = t.merchant or t.raw_description.strip().lower()[:40]
            groups[(merchant, t.amount_cents)].append(t)
    out: list[Insight] = []
    for (merchant, amount), txns in groups.items():
        if len(txns) >= 2 and -amount >= DUP_MIN_CENTS:
            out.append(
                Insight(
                    key=f"dup:{merchant}:{amount}",
                    type="duplicate",
                    severity="warn",
                    title=f"Charged {len(txns)}× by {merchant}",
                    body=(
                        f"{len(txns)} charges of {_money(-amount)} last month. "
                        "Check for a duplicate or an unwanted subscription."
                    ),
                    action="Review charges",
                    amount_cents=-amount * (len(txns) - 1),
                    link=f"/transactions?q={merchant}",
                )
            )
    return out


def _goal_nudges(db: Session, household: models.Household, today: dt.date) -> list[Insight]:
    out: list[Insight] = []
    for g in list_goals(db, household, today):
        if g.complete:
            out.append(
                Insight(
                    key=f"goal:{g.id}", type="goal_nudge", severity="info",
                    title=f"You reached {g.name}! 🎉",
                    body=f"{_money(g.target_cents)} saved. Time to set the next goal?",
                    amount_cents=g.target_cents, link="/goals",
                )
            )
        elif g.target_date and g.target_date < today:
            out.append(
                Insight(
                    key=f"goal:{g.id}", type="goal_nudge", severity="warn",
                    title=f"{g.name} is past its target date",
                    body=(
                        f"{_money(g.remaining_cents)} still to go. "
                        "Adjust the target or top it up."
                    ),
                    action="Update goal", amount_cents=g.remaining_cents, link="/goals",
                )
            )
        elif g.suggested_per_period_cents > 0 and g.target_date:
            by = g.target_date.strftime("%d %b %Y")
            out.append(
                Insight(
                    key=f"goal:{g.id}", type="goal_nudge", severity="info",
                    title=f"Stay on track for {g.name}",
                    body=(
                        f"{_money(g.remaining_cents)} to go by {by}. Set aside "
                        f"{_money(g.suggested_per_period_cents)} per {g.period_label}."
                    ),
                    action="Top up", amount_cents=g.remaining_cents, link="/goals",
                )
            )
    return out


def generate(
    db: Session, household: models.Household, today: dt.date | None = None
) -> InsightsOut:
    today = today or dt.date.today()
    (a_s, a_e), (b_s, b_e), (c_s, c_e) = _month_bounds(today)  # a oldest, c last full month
    names = _category_names(db, household.id)

    leaves_a = _spendable_leaves(db, household.id, a_s, a_e)
    leaves_b = _spendable_leaves(db, household.id, b_s, b_e)
    leaves_c = _spendable_leaves(db, household.id, c_s, c_e)
    spend_a = _spend_by_category(leaves_a)
    spend_b = _spend_by_category(leaves_b)
    spend_c = _spend_by_category(leaves_c)

    creeping = _creeping(spend_a, spend_b, spend_c, names)
    creeping_cats = {i.key.split(":", 1)[1] for i in creeping}

    insights: list[Insight] = []
    insights += _budget_alerts(db, household, today)
    insights += creeping
    insights += _movers(spend_b, spend_c, names, skip=creeping_cats)
    insights += _fees(leaves_c)
    insights += _duplicates(leaves_c)
    insights += _goal_nudges(db, household, today)

    insights.sort(key=lambda i: (_SEVERITY_RANK.get(i.severity, 3), -abs(i.amount_cents or 0)))
    return InsightsOut(generated_for=c_s.strftime("%B %Y"), insights=insights[:MAX_INSIGHTS])
