"""Account CRUD (PRD R2) with computed live balance."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_owner, require_writer
from ..services import networth, repayments, transfers
from ..services.mortgage import set_balance
from ..services.transfers import wording_of

router = APIRouter(prefix="/accounts", tags=["accounts"])


def _to_out(db: Session, account: models.Account) -> schemas.AccountOut:
    total, count = db.execute(
        select(
            func.coalesce(func.sum(models.Transaction.amount_cents), 0),
            func.count(models.Transaction.id),
        ).where(
            models.Transaction.account_id == account.id,
            models.Transaction.split_parent_id.is_(None),
        )
    ).one()
    return schemas.AccountOut(
        id=account.id,
        name=account.name,
        type=account.type,
        institution=account.institution,
        currency=account.currency,
        opening_balance_cents=account.opening_balance_cents,
        owner_user_id=account.owner_user_id,
        balance_cents=account.opening_balance_cents + int(total or 0),
        txn_count=int(count or 0),
        bank_identifier=account.bank_identifier,
        transfer_wording=list(account.transfer_wording or []),
        repayment_schedule=[
            schemas.RepaymentPeriodIn(**p.as_json())
            for p in repayments.schedule_of(account)
        ],
        repayment_suggestion=_suggestion(db, account),
        in_net_worth=bool(account.in_net_worth),
    )


def _suggestion(db: Session, account: models.Account) -> schemas.RepaymentPeriodIn | None:
    found = repayments.suggestion_for(db, account)
    return schemas.RepaymentPeriodIn(**found.as_json()) if found else None


# Shorter wording than this matches too much: "cc" would read a payment to the card into
# every "ACC 1234" description in the household's other accounts.
MIN_WORDING_LETTERS = 4


def _clean_wording(phrases: list[str]) -> list[str]:
    """Trimmed, one of each (compared as wording: case, digits and punctuation aside).
    A phrase with no words, or too short to tell a payment to this account from anything
    else, is refused with the reason."""
    kept: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        text = " ".join(phrase.split())
        key = wording_of(text)
        if not text:
            continue  # a blank line
        if not key:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"“{text}” has no words in it. Use your bank's wording, e.g. "
                "“By Authority To Cc”; the account number is read from your statements.",
            )
        if len(key.replace(" ", "")) < MIN_WORDING_LETTERS:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"“{text}” is too short to recognise a payment by. Use more of your "
                "bank's wording, e.g. “By Authority To Cc”.",
            )
        if key not in seen:
            seen.add(key)
            kept.append(text)
    return kept


def clean_schedule(periods: list[dict]) -> list[dict] | None:
    """Oldest first, one period a date -- a second for the same date replaces the first;
    an undated one is "from the start". Empty is none."""
    by_since: dict[str | None, dict] = {}
    for p in periods:
        by_since[p.get("since")] = p
    return sorted(by_since.values(), key=lambda p: p.get("since") or "") or None


def _get_owned(db: Session, account_id: str, household_id: str) -> models.Account:
    account = db.get(models.Account, account_id)
    if account is None or account.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    return account


@router.get("", response_model=list[schemas.AccountOut])
def list_accounts(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[schemas.AccountOut]:
    accounts = (
        db.execute(
            select(models.Account)
            .where(models.Account.household_id == user.household_id)
            .order_by(models.Account.name)
        )
        .scalars()
        .all()
    )
    return [_to_out(db, a) for a in accounts]


@router.post("", response_model=schemas.AccountOut, status_code=status.HTTP_201_CREATED)
def create_account(
    payload: schemas.AccountCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.AccountOut:
    account = models.Account(household_id=user.household_id, **payload.model_dump())
    db.add(account)
    db.commit()
    db.refresh(account)
    return _to_out(db, account)


@router.get("/{account_id}", response_model=schemas.AccountOut)
def get_account(
    account_id: str,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.AccountOut:
    return _to_out(db, _get_owned(db, account_id, user.household_id))


@router.patch("/{account_id}", response_model=schemas.AccountUpdateOut)
def update_account(
    account_id: str,
    payload: schemas.AccountUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.AccountUpdateOut:
    account = _get_owned(db, account_id, user.household_id)
    balance_on = payload.balance_on or dt.date.today()
    if payload.balance_cents is not None and balance_on > dt.date.today():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "The balance's date can't be in the future."
        )
    old_type = account.type
    for key, value in payload.model_dump(exclude_unset=True, mode="json").items():
        if key in ("balance_cents", "balance_on"):
            continue  # below, once the rest is set
        if key == "transfer_wording":
            value = _clean_wording(value or [])
        elif key == "repayment_schedule":
            value = clean_schedule(value or [])
        elif value is None and key in ("name", "type", "in_net_worth"):
            continue  # an explicit null is "leave it": these columns are not nullable
        setattr(account, key, value)
    # The type decides what money into the account is: what was linked under the old
    # one is counted by the new one.
    rekinded = 0
    if account.type != old_type:
        rekinded = transfers.rekind_into(db, account, old_type)
        transfers.unfile_cash_deposits(db, account)
    if account.type != old_type or "repayment_schedule" in payload.model_fields_set:
        db.flush()
        repayments.sort_repayments(db, user.household_id)
    if payload.balance_cents is not None:
        db.flush()
        set_balance(db, account, payload.balance_cents, balance_on)
    db.commit()
    networth.record_snapshot_if_used(db, user.household_id)
    db.refresh(account)
    return schemas.AccountUpdateOut(
        **_to_out(db, account).model_dump(), transfers_rekinded=rekinded
    )


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: str,
    user: models.User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    account = _get_owned(db, account_id, user.household_id)
    has_txns = db.execute(
        select(models.Transaction.id).where(models.Transaction.account_id == account.id).limit(1)
    ).first()
    if has_txns:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Account has transactions; delete those first."
        )
    db.delete(account)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
