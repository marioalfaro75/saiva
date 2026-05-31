"""Account CRUD (PRD R2) with computed live balance."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_owner, require_writer

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
    )


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


@router.patch("/{account_id}", response_model=schemas.AccountOut)
def update_account(
    account_id: str,
    payload: schemas.AccountUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.AccountOut:
    account = _get_owned(db, account_id, user.household_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, key, value)
    db.commit()
    db.refresh(account)
    return _to_out(db, account)


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
