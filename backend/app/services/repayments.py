"""Home loan repayments: the regular one, and extra ones (PRD: transfers, 0.18.1).

Both count as spending, once, on the account that paid. What tells them apart is the
household's own pattern: the usual amount and how often it goes. A repayment near the
usual amount, a period after the last regular one, is regular. One far from it, or a
second within the same period, is extra -- unless the new amount comes again a period
later, which is the usual changing (a rate rise, say): two in a row and it is the new
usual. A person's own choice for a repayment stays, and teaches the pattern.

Kept in the category: Mortgage for a regular repayment, Extra repayments for an extra.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..constants import (
    EXTRA_REPAYMENT_CATEGORY,
    LOAN_ACCOUNT_TYPES,
    LOAN_REPAYMENT,
    MORTGAGE_CATEGORY,
)
from .categorise import ML_MAX_CONFIDENCE, RULE_CONFIDENCE, set_by_person

USUAL_FROM = 8  # the usual amount and period, from the last few regular repayments
AMOUNT_TOLERANCE = 0.10  # within 10% of the usual amount
# Sooner than this share of a period after the last regular one: the same period. Not a
# half: a fortnightly household's payment a week on is a second one, not the next.
SAME_PERIOD = 0.6
FIRST_PERIOD_DAYS = 5  # before there is a period to go by, sooner than this is the same


@dataclass
class Repayment:
    txn: models.Transaction
    loan_id: str
    extra: bool


def repayment_category_ids(db: Session, household_id: str) -> tuple[str | None, str | None]:
    """(Mortgage, Extra repayments) for the household, by system key."""
    found = dict(
        db.execute(
            select(models.Category.system_key, models.Category.id).where(
                models.Category.household_id == household_id,
                models.Category.system_key.in_([MORTGAGE_CATEGORY, EXTRA_REPAYMENT_CATEGORY]),
            )
        ).tuples().all()
    )
    return found.get(MORTGAGE_CATEGORY), found.get(EXTRA_REPAYMENT_CATEGORY)


def _loans(db: Session, household_id: str, types: Iterable[str]) -> list[models.Account]:
    return list(
        db.execute(
            select(models.Account).where(
                models.Account.household_id == household_id,
                models.Account.type.in_(list(types)),
            )
        ).scalars()
    )


def paying_legs(
    db: Session, household_id: str, loan_ids: list[str]
) -> dict[str, list[models.Transaction]]:
    """Each loan's repayments, as the outflows that paid them, oldest first."""
    if not loan_ids:
        return {}
    arrivals = db.execute(
        select(models.Transaction.transfer_group_id, models.Transaction.account_id).where(
            models.Transaction.household_id == household_id,
            models.Transaction.account_id.in_(loan_ids),
            models.Transaction.transfer_kind == LOAN_REPAYMENT,
            models.Transaction.amount_cents > 0,
            models.Transaction.split_parent_id.is_(None),
        )
    ).tuples().all()
    loan_of = {group: loan for group, loan in arrivals if group}
    by_loan: dict[str, list[models.Transaction]] = {loan: [] for loan in loan_ids}
    if not loan_of:
        return by_loan
    for t in db.execute(
        select(models.Transaction).where(
            models.Transaction.household_id == household_id,
            models.Transaction.transfer_group_id.in_(list(loan_of)),
            models.Transaction.amount_cents < 0,
            models.Transaction.split_parent_id.is_(None),
        )
    ).scalars():
        by_loan[loan_of[t.transfer_group_id or ""]].append(t)
    for legs in by_loan.values():
        legs.sort(key=lambda t: (t.txn_date, t.id))
    return by_loan


def sort_out(
    legs: list[models.Transaction], fixed: dict[str, bool] | None = None
) -> dict[str, bool]:
    """Which of one loan's repayments (oldest first) are extra, by id. `fixed` holds the
    ones a person decided, which are taken as said and teach the pattern."""
    fixed = fixed or {}
    extra: dict[str, bool] = {}
    regular: list[models.Transaction] = []
    pending: models.Transaction | None = None  # on time at a new amount: the usual changing?

    def amount(t: models.Transaction) -> int:
        return abs(t.amount_cents)

    def near(a: int, b: float) -> bool:
        return abs(a - b) <= AMOUNT_TOLERANCE * b

    def same_period_days() -> float:
        recent = regular[-USUAL_FROM:]
        gaps = [(b.txn_date - a.txn_date).days for a, b in zip(recent, recent[1:], strict=False)]
        return statistics.median(gaps) * SAME_PERIOD if gaps else FIRST_PERIOD_DAYS

    for t in legs:
        if t.id in fixed:
            extra[t.id] = fixed[t.id]
            if not fixed[t.id]:
                if regular and not near(
                    amount(t), statistics.median(amount(r) for r in regular[-USUAL_FROM:])
                ):
                    regular = []  # a person's regular one at a new amount: the new usual
                regular.append(t)
                pending = None
            continue
        if not regular:
            extra[t.id] = False
            regular.append(t)
            continue
        usual = statistics.median(amount(r) for r in regular[-USUAL_FROM:])
        period = same_period_days()
        on_time = (t.txn_date - regular[-1].txn_date).days >= period
        if on_time and near(amount(t), usual):
            extra[t.id] = False
            regular.append(t)
            pending = None
        elif (
            pending is not None
            and near(amount(t), amount(pending))
            and (t.txn_date - pending.txn_date).days >= period
        ):
            # The same new amount a period later: the usual has changed.
            extra[pending.id] = extra[t.id] = False
            regular = [pending, t]
            pending = None
        else:
            extra[t.id] = True
            if on_time:
                pending = t
    return extra


def sort_repayments(db: Session, household_id: str) -> int:
    """File each home loan repayment as Mortgage or Extra repayments, by the pattern.
    Only what Saiva filed is moved; a person's or a locked category stays (and a
    person's teaches the pattern). Returns the repayments re-filed. Does not commit."""
    mortgage, extra_id = repayment_category_ids(db, household_id)
    if mortgage is None or extra_id is None:
        return 0
    loans = _loans(db, household_id, ["home_loan"])
    changed = 0
    for legs in paying_legs(db, household_id, [a.id for a in loans]).values():
        fixed = {
            t.id: t.category_id == extra_id
            for t in legs
            if (t.category_locked or set_by_person(t)) and t.category_id in (mortgage, extra_id)
        }
        by_id = {t.id: t for t in legs}
        for t_id, is_extra in sort_out(legs, fixed).items():
            t = by_id[t_id]
            if t_id in fixed or t.category_id not in (mortgage, extra_id):
                continue  # a person's, or filed elsewhere -- a rule's, say
            if (t.confidence or 0) >= RULE_CONFIDENCE:
                continue
            target = extra_id if is_extra else mortgage
            if t.category_id != target:
                t.category_id = target
                t.confidence = ML_MAX_CONFIDENCE
                changed += 1
    return changed


@dataclass
class RepaymentSeries:
    loan: models.Account
    category_id: str | None
    regular: list[models.Transaction]


def regular_repayments(db: Session, household_id: str) -> list[RepaymentSeries]:
    """Each loan's regular repayments, for the bills: extras are not a bill."""
    mortgage, extra_id = repayment_category_ids(db, household_id)
    loans = _loans(db, household_id, LOAN_ACCOUNT_TYPES)
    legs_by_loan = paying_legs(db, household_id, [a.id for a in loans])
    out: list[RepaymentSeries] = []
    for loan in loans:
        legs = legs_by_loan.get(loan.id, [])
        if loan.type == "home_loan":
            regular = [t for t in legs if t.category_id != extra_id]
        else:
            flags = sort_out(legs)
            regular = [t for t in legs if not flags.get(t.id)]
        if regular:
            category = regular[-1].category_id or mortgage
            out.append(RepaymentSeries(loan, category, regular))
    return out

