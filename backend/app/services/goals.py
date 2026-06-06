"""Savings goals (PRD R25): a target amount with an optional deadline and an
optional linked account. Progress is the linked account's balance when set,
otherwise a manually tracked amount; the suggested per-pay contribution is
derived from how many of the household's pay cycles remain before the deadline."""

from __future__ import annotations

import datetime as dt
import math

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import SavingsGoalOut


def _account_balance(db: Session, account: models.Account) -> int:
    total = db.execute(
        select(func.coalesce(func.sum(models.Transaction.amount_cents), 0)).where(
            models.Transaction.account_id == account.id,
            models.Transaction.split_parent_id.is_(None),
        )
    ).scalar_one()
    return account.opening_balance_cents + int(total or 0)


def _period_info(basis: str) -> tuple[int | None, str]:
    if basis == "weekly":
        return 7, "week"
    if basis == "fortnightly":
        return 14, "fortnight"
    return None, "month"


def _months_between(start: dt.date, end: dt.date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def _suggested(remaining: int, target_date: dt.date | None, basis: str, today: dt.date) -> int:
    if remaining <= 0 or target_date is None or target_date <= today:
        return 0
    days, _ = _period_info(basis)
    if days:
        periods = max(1, math.ceil((target_date - today).days / days))
    else:
        periods = max(1, _months_between(today, target_date))
    return math.ceil(remaining / periods)


def _build_out(
    db: Session,
    goal: models.SavingsGoal,
    accounts: dict[str, models.Account],
    basis: str,
    today: dt.date,
) -> SavingsGoalOut:
    account = accounts.get(goal.account_id) if goal.account_id else None
    current = max(0, _account_balance(db, account) if account else goal.current_cents)
    remaining = max(0, goal.target_cents - current)
    pct = round(min(current / goal.target_cents, 1.0), 4) if goal.target_cents > 0 else 0.0
    _, label = _period_info(basis)
    return SavingsGoalOut(
        id=goal.id,
        name=goal.name,
        target_cents=goal.target_cents,
        target_date=goal.target_date,
        account_id=goal.account_id,
        account_name=account.name if account else None,
        current_cents=current,
        remaining_cents=remaining,
        pct_complete=pct,
        suggested_per_period_cents=_suggested(remaining, goal.target_date, basis, today),
        period_label=label,
        complete=current >= goal.target_cents,
    )


def _accounts(db: Session, household_id: str) -> dict[str, models.Account]:
    return {
        a.id: a
        for a in db.execute(
            select(models.Account).where(models.Account.household_id == household_id)
        )
        .scalars()
        .all()
    }


def list_goals(
    db: Session, household: models.Household, today: dt.date | None = None
) -> list[SavingsGoalOut]:
    today = today or dt.date.today()
    goals = (
        db.execute(
            select(models.SavingsGoal)
            .where(models.SavingsGoal.household_id == household.id)
            .order_by(models.SavingsGoal.sort, models.SavingsGoal.name)
        )
        .scalars()
        .all()
    )
    accounts = _accounts(db, household.id)
    return [_build_out(db, g, accounts, household.period_basis, today) for g in goals]


def compute_goal(
    db: Session,
    household: models.Household,
    goal: models.SavingsGoal,
    today: dt.date | None = None,
) -> SavingsGoalOut:
    today = today or dt.date.today()
    return _build_out(db, goal, _accounts(db, household.id), household.period_basis, today)
