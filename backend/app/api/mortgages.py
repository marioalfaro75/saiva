"""The Mortgage page (0.19.0): a home loan's setup, and where it is going.

GET returns the loan with its projection; the what-if is in the query (extra each
repayment, a lump sum now, a change of rate, a change in the offset), so trying one
saves nothing. PUT saves the setup: it also sets what the loan account and its offsets
read, and counts them in net worth."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import mortgage as mortgage_service
from ..services import networth, repayments
from ..services.balances import BalanceTooLarge, set_balance
from .accounts import clean_schedule, latest_today

# A liability entered by hand on Net worth with one of these in its name may be this loan.
LIKE_A_HOME_LOAN = re.compile(r"mortgage|home|house|property", re.IGNORECASE)
SAME_LOAN_SHARE = 0.2  # or one for within a fifth of what this loan owes

router = APIRouter(prefix="/mortgages", tags=["mortgages"])

# What can offset a home loan: an account that holds money.
OFFSET_TYPES = ("offset", "everyday", "savings")
MAX_CENTS = schemas.MAX_BALANCE_CENTS


def _home_loans(db: Session, household_id: str) -> list[models.Account]:
    return list(
        db.execute(
            select(models.Account)
            .where(
                models.Account.household_id == household_id,
                models.Account.type == "home_loan",
            )
            .order_by(models.Account.name)
        ).scalars()
    )


def _loan(db: Session, account_id: str, household_id: str) -> models.Account:
    account = db.get(models.Account, account_id)
    if account is None or account.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    if account.type != "home_loan":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "That account isn't a home loan."
        )
    return account


def _setup(db: Session, loan: models.Account) -> models.Mortgage | None:
    return db.execute(
        select(models.Mortgage).where(models.Mortgage.account_id == loan.id)
    ).scalar_one_or_none()


def _offset_choices(db: Session, household_id: str) -> list[models.Account]:
    return list(
        db.execute(
            select(models.Account)
            .where(
                models.Account.household_id == household_id,
                models.Account.type.in_(OFFSET_TYPES),
            )
            .order_by(models.Account.name)
        ).scalars()
    )


def _like_this_loan(db: Session, loan: models.Account) -> list[str]:
    """Liabilities entered by hand on Net worth that may be this loan: named like a home
    loan, or for about what it owes."""
    owing = max(-mortgage_service.balance_through(db, loan, None), 0)
    return [
        item.name
        for item in db.execute(
            select(models.NetWorthItem)
            .where(
                models.NetWorthItem.household_id == loan.household_id,
                models.NetWorthItem.kind == "liability",
            )
            .order_by(models.NetWorthItem.name)
        ).scalars()
        if LIKE_A_HOME_LOAN.search(item.name)
        or (owing and abs(item.value_cents - owing) <= SAME_LOAN_SHARE * owing)
    ]


def _detail(
    db: Session, loan: models.Account, what_if: mortgage_service.WhatIf | None = None
) -> schemas.MortgageOut:
    today = dt.date.today()
    setup = _setup(db, loan)
    known_on, _ = mortgage_service.known_owing(db, loan, today)
    past = mortgage_service.history(db, loan, today, known_on)
    suggestion = repayments.suggestion_for(db, loan, today)
    out = schemas.MortgageOut(
        account_id=loan.id,
        name=loan.name,
        set_up=setup is not None,
        repayment_schedule=[
            schemas.RepaymentPeriodIn(**p.as_json()) for p in repayments.schedule_of(loan)
        ],
        repayment_suggestion=(
            schemas.RepaymentPeriodIn(**suggestion.as_json()) if suggestion else None
        ),
        offset_choices=[
            schemas.MortgageOffsetOut(
                account_id=a.id,
                name=a.name,
                balance_cents=mortgage_service.balance_through(db, a, None),
            )
            for a in _offset_choices(db, loan.household_id)
        ],
        paid=[schemas.MortgagePaidMonth(**dataclasses.asdict(m)) for m in past.months],
        manual_liabilities=_like_this_loan(db, loan),
    )
    if setup is None:
        out.missing = ["setup"]
        return out
    names = {a.account_id: a.name for a in out.offset_choices}
    out.original_cents = setup.original_cents
    out.start_date = setup.start_date
    out.term_months = setup.term_months
    out.rates = [
        schemas.MortgageRate(since=r.since, percent=r.percent)
        for r in mortgage_service.rates_of(setup)
    ]
    out.offsets = [
        schemas.MortgageOffsetOut(account_id=i, name=names.get(i, "An account"), balance_cents=b)
        for i, b in mortgage_service.offset_balance(db, setup).items()
    ]
    out.balance_as_of = loan.balance_anchor_on
    out.missing = mortgage_service.missing(setup, loan)
    projection = mortgage_service.project(db, setup, loan, what_if, today, past)
    if projection is not None:
        out.projection = schemas.MortgageProjectionOut(**dataclasses.asdict(projection))
    return out


@router.get("", response_model=list[schemas.MortgageLoanOut])
def list_loans(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[schemas.MortgageLoanOut]:
    """The household's home loans, and which are set up."""
    return [
        schemas.MortgageLoanOut(account_id=a.id, name=a.name, set_up=_setup(db, a) is not None)
        for a in _home_loans(db, user.household_id)
    ]


@router.get("/{account_id}", response_model=schemas.MortgageOut)
def get_mortgage(
    account_id: str,
    extra_cents: int = Query(0, ge=0, le=MAX_CENTS),
    lump_cents: int = Query(0, ge=0, le=MAX_CENTS),
    rate_change: float = Query(0.0, ge=-10, le=10),
    offset_change_cents: int = Query(0, ge=-MAX_CENTS, le=MAX_CENTS),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.MortgageOut:
    loan = _loan(db, account_id, user.household_id)
    what_if = mortgage_service.WhatIf(extra_cents, lump_cents, rate_change, offset_change_cents)
    return _detail(db, loan, what_if)


def _clean_rates(rates: list[schemas.MortgageRate]) -> list[dict] | None:
    """Oldest first, one a date: a second for the same date replaces the first."""
    by_since: dict[dt.date | None, schemas.MortgageRate] = {r.since: r for r in rates}
    ordered = sorted(by_since.values(), key=lambda r: r.since or dt.date.min)
    return [
        {"since": r.since.isoformat() if r.since else None, "percent": round(r.percent, 3)}
        for r in ordered
    ] or None


@router.put("/{account_id}", response_model=schemas.MortgageOut)
def save_mortgage(
    account_id: str,
    payload: schemas.MortgageIn,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.MortgageOut:
    loan = _loan(db, account_id, user.household_id)
    today = payload.today or dt.date.today()
    if today > latest_today() or today < dt.date.today() - dt.timedelta(days=1):
        today = dt.date.today()  # not a today anywhere: the server's
    if payload.owing_on is not None and payload.owing_on > latest_today():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "The date for what you owe can't be in the future.",
        )
    choices = {a.id: a for a in _offset_choices(db, user.household_id)}
    offsets: list[models.Account] = []
    for o in payload.offsets:
        account = choices.get(o.account_id)
        if account is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "An offset must be one of your everyday, savings or offset accounts.",
            )
        if account not in offsets:
            offsets.append(account)
    setup = _setup(db, loan)
    if setup is None:
        setup = models.Mortgage(household_id=user.household_id, account_id=loan.id)
        db.add(setup)
    setup.original_cents = payload.original_cents
    setup.start_date = payload.start_date
    setup.term_months = payload.term_months
    setup.rates = _clean_rates(payload.rates)
    setup.offset_account_ids = [a.id for a in offsets] or None
    db.flush()
    try:
        if payload.owing_cents is not None:
            set_balance(db, loan, -payload.owing_cents, payload.owing_on or today)
        for o in payload.offsets:
            if o.balance_cents is not None:
                set_balance(db, choices[o.account_id], o.balance_cents, today)
    except BalanceTooLarge as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    if payload.repayment_schedule is not None:
        loan.repayment_schedule = clean_schedule(
            [p.model_dump(mode="json") for p in payload.repayment_schedule]
        )
        db.flush()
        repayments.sort_repayments(db, user.household_id)
    # Its balances are right now: they count.
    loan.in_net_worth = True
    for account in offsets:
        account.in_net_worth = True
    db.commit()
    networth.record_snapshot_if_used(db, user.household_id)
    return _detail(db, loan)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mortgage(
    account_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> Response:
    """Forget the setup. The accounts, their balances and repayments stay."""
    loan = _loan(db, account_id, user.household_id)
    setup = _setup(db, loan)
    if setup is not None:
        db.delete(setup)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
