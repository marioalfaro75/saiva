"""Transaction explorer: search/filter/edit, recategorise (+learn), split, manual,
delete (PRD R13, R15, R20)."""

from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import Integer, String, case, cast, false, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, optional_period, require_writer
from ..services import periods
from ..services.categorise import build_categoriser
from ..services.importers import dedup_hash
from ..services.merchants import normalise_merchant
from ..services.transfers import detect_transfers

router = APIRouter(prefix="/transactions", tags=["transactions"])


def _cat_names(db: Session, household_id: str) -> dict[str, str]:
    return {
        c.id: c.name
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household_id)
        )
        .scalars()
        .all()
    }


def _acct_names(db: Session, household_id: str) -> dict[str, str]:
    return {
        a.id: a.name
        for a in db.execute(
            select(models.Account).where(models.Account.household_id == household_id)
        )
        .scalars()
        .all()
    }


def _to_out(
    t: models.Transaction, cats: dict[str, str], accts: dict[str, str]
) -> schemas.TransactionOut:
    return schemas.TransactionOut(
        id=t.id,
        account_id=t.account_id,
        account_name=accts.get(t.account_id),
        txn_date=t.txn_date,
        amount_cents=t.amount_cents,
        raw_description=t.raw_description,
        merchant=t.merchant,
        category_id=t.category_id,
        category_name=cats.get(t.category_id) if t.category_id else None,
        is_transfer=t.is_transfer,
        is_recurring=t.is_recurring,
        category_locked=t.category_locked,
        confidence=t.confidence,
        source=t.source,
        notes=t.notes,
        tags=list(t.tags or []),
        split_parent_id=t.split_parent_id,
    )


def _get_owned(db: Session, txn_id: str, household_id: str) -> models.Transaction:
    t = db.get(models.Transaction, txn_id)
    if t is None or t.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found")
    return t


# Sortable columns, allow-listed: the key selects a column here and is never
# interpolated into SQL. Account and category sort by name, which is what the table
# shows, so both need a join.
SORTABLE = {
    "date": models.Transaction.txn_date,
    "description": models.Transaction.merchant,
    "account": models.Account.name,
    "category": models.Category.name,
    "amount": models.Transaction.amount_cents,
}

# Per-column filters, matching the columns the transactions table displays.
FILTERABLE = {
    "description": (models.Transaction.raw_description, models.Transaction.merchant),
    "account": (models.Account.name,),
    "category": (models.Category.name,),
}


def _amount_condition(term: str) -> ColumnElement[bool] | None:
    """Match an amount the way the table displays it.

    Amounts are stored in cents, so a text match on the raw column would make "12.50"
    fail to find 1250. Dividing by 100.0 and casting is no better — the database
    renders that as "85.4", so "85.40" would still miss. Build "85.40" from integer
    arithmetic instead, which formats identically on SQLite and Postgres.
    """
    digits = re.sub(r"[^\d.]", "", term)
    if not digits:
        return None
    cents = func.abs(models.Transaction.amount_cents)
    # floor() and %, not a plain cast: SQLAlchemy renders `/` as true division, and
    # Postgres rounds when casting the result to an integer — so 8560 would give a
    # whole part of 86 and a remainder of -40, rendering "86.-40". SQLite truncates,
    # which would have hidden it.
    whole = cast(func.floor(cents / 100.0), Integer)
    frac = cents % 100
    padded = case((frac < 10, "0" + cast(frac, String)), else_=cast(frac, String))
    formatted = cast(whole, String) + "." + padded
    # "85", "85.4", "5.40" and "85.40" all match -$85.40.
    return formatted.like(f"%{digits}%")


def _sorted(stmt: Select, sort: str | None, direction: str) -> Select:
    """Apply ordering. Always ends with a unique-ish tiebreaker so paging through a
    sorted list cannot repeat or skip rows when several share a sort value."""
    tiebreak = (models.Transaction.txn_date.desc(), models.Transaction.created_at.desc())
    if sort is None:
        return stmt.order_by(*tiebreak)
    column = SORTABLE[sort]
    # Blank values sort last whichever direction is chosen, matching the client-side
    # tables, so an uncategorised row never tops the list.
    ordered = column.asc() if direction == "asc" else column.desc()
    return stmt.order_by(column.is_(None), ordered, *tiebreak)


@router.get("", response_model=schemas.TransactionListOut)
def list_transactions(
    q: str | None = None,
    account_id: str | None = None,
    category_id: str | None = None,
    uncategorised: bool = False,
    include_transfers: bool = True,
    start: dt.date | None = None,
    end: dt.date | None = None,
    min_amount_cents: int | None = None,
    max_amount_cents: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str | None = None,
    dir: str = "asc",
    f_description: str | None = None,
    f_account: str | None = None,
    f_category: str | None = None,
    f_amount: str | None = None,
    f_date: str | None = None,
    window: periods.ResolvedPeriod | None = Depends(optional_period),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.TransactionListOut:
    if sort is not None and sort not in SORTABLE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Cannot sort by '{sort}'"
        )
    if dir not in ("asc", "desc"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "dir must be asc or desc")
    # The global period picker sets the window; explicit start/end still work on
    # their own for callers that filter by raw dates.
    if window is not None:
        start, end = window.start, window.end
    conditions = [models.Transaction.household_id == user.household_id]
    if account_id:
        conditions.append(models.Transaction.account_id == account_id)
    if category_id:
        conditions.append(models.Transaction.category_id == category_id)
    if uncategorised:
        conditions.append(models.Transaction.category_id.is_(None))
    if not include_transfers:
        conditions.append(models.Transaction.is_transfer.is_(False))
    if start:
        conditions.append(models.Transaction.txn_date >= start)
    if end:
        conditions.append(models.Transaction.txn_date <= end)
    if min_amount_cents is not None:
        conditions.append(models.Transaction.amount_cents >= min_amount_cents)
    if max_amount_cents is not None:
        conditions.append(models.Transaction.amount_cents <= max_amount_cents)
    if q:
        # Free-text search spans every column the table shows, not just the description.
        like = f"%{q}%"
        anywhere: list[ColumnElement[bool]] = [
            models.Transaction.raw_description.ilike(like),
            models.Transaction.merchant.ilike(like),
            models.Transaction.notes.ilike(like),
            models.Account.name.ilike(like),
            models.Category.name.ilike(like),
            cast(models.Transaction.txn_date, String).like(like),
        ]
        amount_match = _amount_condition(q)
        if amount_match is not None:
            anywhere.append(amount_match)
        conditions.append(or_(*anywhere))

    for name, columns in FILTERABLE.items():
        term = {"description": f_description, "account": f_account, "category": f_category}[name]
        if term:
            conditions.append(or_(*[c.ilike(f"%{term}%") for c in columns]))
    if f_amount:
        amount_match = _amount_condition(f_amount)
        # A filter of pure punctuation matches nothing rather than everything.
        conditions.append(amount_match if amount_match is not None else false())
    if f_date:
        conditions.append(cast(models.Transaction.txn_date, String).like(f"%{f_date}%"))

    # Left joins: sorting or filtering by account/category must never drop rows that
    # have neither, which an inner join would do silently.
    def joined(stmt: Select) -> Select:
        return stmt.join(models.Account, isouter=True).join(models.Category, isouter=True)

    total = db.execute(
        joined(select(func.count(models.Transaction.id))).where(*conditions)
    ).scalar_one()
    rows = (
        db.execute(
            _sorted(joined(select(models.Transaction)).where(*conditions), sort, dir)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        .scalars()
        .all()
    )
    cats = _cat_names(db, user.household_id)
    accts = _acct_names(db, user.household_id)
    return schemas.TransactionListOut(
        items=[_to_out(t, cats, accts) for t in rows],
        total=int(total),
        page=page,
        page_size=page_size,
    )


@router.post("/detect-transfers")
def run_detect_transfers(
    user: models.User = Depends(require_writer), db: Session = Depends(get_db)
) -> dict[str, int]:
    return {"linked": detect_transfers(db, user.household_id)}


@router.post("", response_model=schemas.TransactionOut, status_code=status.HTTP_201_CREATED)
def create_manual(
    payload: schemas.ManualTxnCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.TransactionOut:
    account = db.get(models.Account, payload.account_id)
    if account is None or account.household_id != user.household_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown account")

    merchant = normalise_merchant(payload.description)
    category_id = payload.category_id
    confidence: float | None = None
    if category_id is None:
        result = build_categoriser(db, user.household_id).categorise(payload.description, merchant)
        category_id = result.category_id
        confidence = result.confidence if category_id else None

    txn = models.Transaction(
        household_id=user.household_id,
        account_id=payload.account_id,
        txn_date=payload.txn_date,
        amount_cents=payload.amount_cents,
        raw_description=payload.description,
        merchant=merchant,
        category_id=category_id,
        confidence=confidence,
        source="manual",
        notes=payload.notes,
        tags=payload.tags,
        dedup_hash=dedup_hash(
            payload.account_id, payload.txn_date, payload.amount_cents, payload.description
        ),
    )
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return _to_out(txn, _cat_names(db, user.household_id), _acct_names(db, user.household_id))


@router.patch("/{txn_id}", response_model=schemas.TransactionOut)
def update_txn(
    txn_id: str,
    payload: schemas.TransactionUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.TransactionOut:
    t = _get_owned(db, txn_id, user.household_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(t, key, value)
    db.commit()
    db.refresh(t)
    return _to_out(t, _cat_names(db, user.household_id), _acct_names(db, user.household_id))


def _scope_targets(
    db: Session, household_id: str, t: models.Transaction, scope: str, pattern: str | None
) -> list[models.Transaction]:
    """Other transactions to also update for an explicit apply-to-similar. Locked
    rows are excluded so an exemption is always respected."""
    if scope == "none":
        return []
    base = select(models.Transaction).where(
        models.Transaction.household_id == household_id,
        models.Transaction.category_locked.is_(False),
        models.Transaction.id != t.id,
    )
    if scope == "merchant":
        if not t.merchant:
            return []
        query = base.where(models.Transaction.merchant == t.merchant)
    elif scope == "exact":
        query = base.where(models.Transaction.raw_description == t.raw_description)
    elif scope == "contains":
        text = pattern or t.merchant or t.raw_description
        like = f"%{text}%"
        query = base.where(
            or_(
                models.Transaction.raw_description.ilike(like),
                models.Transaction.merchant.ilike(like),
            )
        )
    else:
        return []
    return list(db.execute(query).scalars().all())


def _rule_from_scope(
    t: models.Transaction, scope: str, pattern: str | None
) -> tuple[str, str] | None:
    """(match_type, pattern) for a persisted rule mirroring the chosen scope."""
    if scope == "exact":
        return "equals", t.raw_description
    if scope == "contains":
        text = pattern or t.merchant or t.raw_description
        return ("contains", text) if text else None
    # "merchant" or "none": prefer a merchant contains-rule, fall back to description
    text = t.merchant or t.raw_description
    return ("contains", text) if text else None


@router.post("/bulk-categorise", response_model=schemas.CountOut)
def bulk_categorise(
    payload: schemas.BulkCategoriseRequest,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.CountOut:
    if payload.category_id is not None:
        category = db.get(models.Category, payload.category_id)
        if category is None or category.household_id != user.household_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown category")
    rows = (
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == user.household_id,
                models.Transaction.id.in_(payload.ids),
            )
        )
        .scalars()
        .all()
    )
    for t in rows:
        if payload.set_category:
            t.category_id = payload.category_id
            t.confidence = 1.0 if payload.category_id else None
        if payload.lock is not None:
            t.category_locked = payload.lock
    db.commit()
    return schemas.CountOut(updated=len(rows))


@router.get("/groups", response_model=schemas.TxnGroupsOut)
def transaction_groups(
    by: Literal["merchant", "description"] = "merchant",
    uncategorised: bool = True,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.TxnGroupsOut:
    column = (
        models.Transaction.merchant if by == "merchant" else models.Transaction.raw_description
    )
    conditions = [
        models.Transaction.household_id == user.household_id,
        models.Transaction.is_transfer.is_(False),
        models.Transaction.split_parent_id.is_(None),
    ]
    if uncategorised:
        conditions.append(models.Transaction.category_id.is_(None))
    rows = db.execute(
        select(
            column,
            func.count(models.Transaction.id),
            func.sum(models.Transaction.amount_cents),
            func.min(models.Transaction.raw_description),
            func.min(models.Transaction.id),
        )
        .where(*conditions)
        .group_by(column)
        .order_by(func.count(models.Transaction.id).desc())
        .limit(200)
    ).all()
    groups = [
        schemas.TxnGroup(
            key=key or "",
            sample_id=sample_id,
            sample_description=sample or (key or ""),
            count=int(count),
            total_cents=int(total or 0),
        )
        for key, count, total, sample, sample_id in rows
        if key
    ]
    return schemas.TxnGroupsOut(by=by, groups=groups)


@router.post("/{txn_id}/recategorise", response_model=schemas.RecategoriseOut)
def recategorise(
    txn_id: str,
    payload: schemas.RecategoriseRequest,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.RecategoriseOut:
    t = _get_owned(db, txn_id, user.household_id)
    if payload.category_id is not None:
        category = db.get(models.Category, payload.category_id)
        if category is None or category.household_id != user.household_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown category")

    t.category_id = payload.category_id
    t.confidence = 1.0 if payload.category_id else None
    if payload.lock:
        t.category_locked = True
    updated = 1

    for s in _scope_targets(db, user.household_id, t, payload.scope, payload.pattern):
        s.category_id = payload.category_id
        s.confidence = 1.0 if payload.category_id else None
        if payload.lock:
            s.category_locked = True
        updated += 1

    if payload.make_rule and payload.category_id:
        rule = _rule_from_scope(t, payload.scope, payload.pattern)
        if rule is not None:
            db.add(
                models.CategorisationRule(
                    household_id=user.household_id,
                    match_type=rule[0],
                    pattern=rule[1].lower(),
                    category_id=payload.category_id,
                    priority=10,
                    source="user",
                )
            )

    db.commit()
    db.refresh(t)
    out = _to_out(t, _cat_names(db, user.household_id), _acct_names(db, user.household_id))
    return schemas.RecategoriseOut(transaction=out, updated_count=updated)


@router.post("/{txn_id}/split", response_model=list[schemas.TransactionOut])
def split_txn(
    txn_id: str,
    payload: schemas.SplitRequest,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> list[schemas.TransactionOut]:
    parent = _get_owned(db, txn_id, user.household_id)
    if parent.split_parent_id is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot split a split line")
    if sum(s.amount_cents for s in payload.splits) != parent.amount_cents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Splits must sum to the original amount")

    for child in (
        db.execute(
            select(models.Transaction).where(models.Transaction.split_parent_id == parent.id)
        )
        .scalars()
        .all()
    ):
        db.delete(child)

    children: list[models.Transaction] = []
    for i, s in enumerate(payload.splits):
        child = models.Transaction(
            household_id=user.household_id,
            account_id=parent.account_id,
            txn_date=parent.txn_date,
            amount_cents=s.amount_cents,
            raw_description=parent.raw_description,
            merchant=parent.merchant,
            category_id=s.category_id,
            source="manual",
            notes=s.notes,
            split_parent_id=parent.id,
            dedup_hash=dedup_hash(
                parent.account_id, parent.txn_date, s.amount_cents,
                f"{parent.raw_description}#split{i}",
            ),
        )
        db.add(child)
        children.append(child)

    db.commit()
    cats = _cat_names(db, user.household_id)
    accts = _acct_names(db, user.household_id)
    for child in children:
        db.refresh(child)
    return [_to_out(c, cats, accts) for c in children]


@router.delete("/{txn_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_txn(
    txn_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> Response:
    t = _get_owned(db, txn_id, user.household_id)
    for child in (
        db.execute(select(models.Transaction).where(models.Transaction.split_parent_id == t.id))
        .scalars()
        .all()
    ):
        db.delete(child)
    # Release the other leg. A transfer group describes two rows being the same money;
    # with one gone the survivor is an ordinary transaction again, and leaving it
    # flagged kept it out of every total with nothing left on screen to explain why.
    if t.transfer_group_id:
        for other in (
            db.execute(
                select(models.Transaction).where(
                    models.Transaction.household_id == user.household_id,
                    models.Transaction.transfer_group_id == t.transfer_group_id,
                    models.Transaction.id != t.id,
                )
            )
            .scalars()
            .all()
        ):
            other.is_transfer = False
            other.transfer_group_id = None
    db.delete(t)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
