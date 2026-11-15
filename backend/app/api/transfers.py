"""Transfers between the household's own accounts: what was found, what waits for a
person, and the person's decisions -- accept, dismiss, link by hand, undo, re-kind."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import transfers
from .transactions import _acct_names, _cat_names, _to_out

router = APIRouter(prefix="/transfers", tags=["transfers"])

# What the review list shows at most: the newest first. A household with years of
# unlinked equal amounts would otherwise be handed all of them at once.
SUGGESTION_LIMIT = 200


def _owned(db: Session, txn_id: str, household_id: str) -> models.Transaction:
    t = db.get(models.Transaction, txn_id)
    if t is None or t.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found")
    return t


def _group_view(
    group_id: str,
    legs: list[models.Transaction],
    cats: dict[str, str],
    accts: dict[str, str],
) -> schemas.TransferGroupOut | None:
    pair = transfers.outflow_and_inflow(legs)
    if pair is None:
        return None
    out, inflow = pair
    return schemas.TransferGroupOut(
        group_id=group_id,
        kind=out.transfer_kind,
        outflow=_to_out(out, cats, accts, inflow),
        inflow=_to_out(inflow, cats, accts, out),
    )


def _group_out(
    db: Session, household_id: str, group_id: str
) -> schemas.TransferGroupOut:
    legs = transfers.group_legs(db, household_id, group_id)
    view = _group_view(group_id, legs, _cat_names(db, household_id), _acct_names(db, household_id))
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    return view


def _is_split(db: Session, t: models.Transaction) -> bool:
    """Split into parts: the parts are what count, so the whole can't be one leg."""
    return (
        db.execute(
            select(models.Transaction.id).where(models.Transaction.split_parent_id == t.id).limit(1)
        ).first()
        is not None
    )


def _pair(
    db: Session, household_id: str, first_id: str, second_id: str
) -> tuple[models.Transaction, models.Transaction]:
    """The two as (money out, money in): different accounts, opposite directions, and
    neither already part of another pair."""
    a, b = _owned(db, first_id, household_id), _owned(db, second_id, household_id)
    if a.id == b.id or a.account_id == b.account_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "A transfer is between two different accounts."
        )
    if (a.amount_cents < 0) == (b.amount_cents < 0) or 0 in (a.amount_cents, b.amount_cents):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A transfer needs money going out of one account and into the other.",
        )
    if a.transfer_group_id or b.transfer_group_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "One of these is already linked; unlink it first."
        )
    if a.split_parent_id or b.split_parent_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Part of a split can't be linked.")
    if _is_split(db, a) or _is_split(db, b):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "A split transaction can't be linked: undo the split first.",
        )
    return (a, b) if a.amount_cents < 0 else (b, a)


def _link_learned(
    db: Session, household_id: str, destination: models.Account, learned: str
) -> int:
    """What the wording now explains is linked at once, rather than left in the list.
    Counted: the pairs into this account the learned wording explains. The accept itself
    is already saved, so a failure here costs only the follow-up."""
    try:
        scan = transfers.classify_transfers(db, household_id)
    except Exception:  # noqa: BLE001 - the accept stands; a later scan links the rest
        db.rollback()
        return 0
    return sum(
        1
        for p in scan.linked
        if p.destination_id == destination.id and transfers.has_phrase(p.outflow_words, learned)
    )


def _resettle(db: Session, household_id: str, *dates: dt.date) -> None:
    """After a person's decision -- accept, link, unlink, dismiss -- look again around
    it: an inflow no longer held back may be a cash deposit after all, and another may
    now be the one offered."""
    try:
        transfers.classify_transfers(db, household_id, since=min(dates))
    except Exception:  # noqa: BLE001 - the decision stands; the next scan settles it
        db.rollback()


@router.get("/suggestions", response_model=list[schemas.TransferSuggestionOut])
def list_suggestions(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[schemas.TransferSuggestionOut]:
    """Pairs not yet linked, for a person. Almost always the ones nothing but the amount
    and dates (or one side's wording) ties together; one with evidence shows here only
    until the next scan links it -- a transaction added by hand, say."""
    pairs = transfers.find_pairs(db, user.household_id)
    pairs.sort(key=lambda p: (p.outflow.txn_date, p.outflow.id), reverse=True)
    cats, accts = _cat_names(db, user.household_id), _acct_names(db, user.household_id)
    return [
        schemas.TransferSuggestionOut(
            outflow=_to_out(p.outflow, cats, accts, p.inflow),
            inflow=_to_out(p.inflow, cats, accts, p.outflow),
            kind=p.kind,
            days_apart=abs((p.inflow.txn_date - p.outflow.txn_date).days),
        )
        for p in pairs[:SUGGESTION_LIMIT]
    ]


@router.post("/accept", response_model=schemas.TransferAcceptOut)
def accept(
    payload: schemas.TransferAcceptIn,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.TransferAcceptOut:
    out, inflow = _pair(db, user.household_id, payload.outflow_id, payload.inflow_id)
    destination = db.get(models.Account, inflow.account_id)
    if destination is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    kind = payload.kind or transfers.kind_for(destination)
    transfers.apply_kind(db, out, inflow, kind)
    learned = transfers.learn_wording(destination, out.raw_description) if payload.learn else None
    db.commit()
    group_id, dates = out.transfer_group_id or "", (out.txn_date, inflow.txn_date)
    if learned:
        also = _link_learned(db, user.household_id, destination, learned)
    else:
        also = 0
        _resettle(db, user.household_id, *dates)
    return schemas.TransferAcceptOut(
        group=_group_out(db, user.household_id, group_id),
        learned=learned,
        learned_for=destination.name if learned else None,
        also_linked=also,
    )


@router.post("/dismiss", status_code=status.HTTP_204_NO_CONTENT)
def dismiss(
    payload: schemas.TransferPairIn,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> None:
    """Not one transfer: never suggested or linked again."""
    out, inflow = _pair(db, user.household_id, payload.outflow_id, payload.inflow_id)
    transfers.dismiss(db, user.household_id, out.id, inflow.id)
    db.commit()
    _resettle(db, user.household_id, out.txn_date, inflow.txn_date)


@router.post("/link", response_model=schemas.TransferGroupOut)
def link(
    payload: schemas.TransferLinkIn,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.TransferGroupOut:
    """Two transactions a person says are one amount of money moving."""
    out, inflow = _pair(db, user.household_id, payload.first_id, payload.second_id)
    destination = db.get(models.Account, inflow.account_id)
    if destination is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found")
    transfers.apply_kind(db, out, inflow, payload.kind or transfers.kind_for(destination))
    db.commit()
    group_id = out.transfer_group_id or ""
    _resettle(db, user.household_id, out.txn_date, inflow.txn_date)
    return _group_out(db, user.household_id, group_id)


@router.get("/candidates/{txn_id}", response_model=list[schemas.TransactionOut])
def link_candidates(
    txn_id: str,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[schemas.TransactionOut]:
    """What this transaction could be linked to by hand: the other direction, another
    account, not already linked, within two weeks -- equal amounts first."""
    t = _owned(db, txn_id, user.household_id)
    if t.split_parent_id is not None or _is_split(db, t):
        return []  # a split, or part of one, can't be linked: nothing to offer
    part = aliased(models.Transaction)  # a split's part: a split whole is not offered
    rows = (
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == user.household_id,
                models.Transaction.account_id != t.account_id,
                models.Transaction.transfer_group_id.is_(None),
                models.Transaction.split_parent_id.is_(None),
                ~select(part.id).where(part.split_parent_id == models.Transaction.id).exists(),
                models.Transaction.txn_date >= t.txn_date - dt.timedelta(days=14),
                models.Transaction.txn_date <= t.txn_date + dt.timedelta(days=14),
                (
                    models.Transaction.amount_cents > 0
                    if t.amount_cents < 0
                    else models.Transaction.amount_cents < 0
                ),
            )
        )
        .scalars()
        .all()
    )
    rows = sorted(
        rows,
        key=lambda r: (
            abs(abs(r.amount_cents) - abs(t.amount_cents)),
            abs((r.txn_date - t.txn_date).days),
            r.id,
        ),
    )[:20]
    cats, accts = _cat_names(db, user.household_id), _acct_names(db, user.household_id)
    return [_to_out(r, cats, accts) for r in rows]


@router.get("/recent", response_model=list[schemas.TransferGroupOut])
def recent(
    limit: int = Query(30, ge=1, le=200),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[schemas.TransferGroupOut]:
    """Linked pairs, newest first: to check what was linked, and undo one."""
    outs = (
        db.execute(
            select(models.Transaction)
            .where(
                models.Transaction.household_id == user.household_id,
                models.Transaction.transfer_group_id.is_not(None),
                models.Transaction.split_parent_id.is_(None),
                models.Transaction.amount_cents < 0,
            )
            .order_by(models.Transaction.txn_date.desc(), models.Transaction.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    group_ids = [o.transfer_group_id for o in outs if o.transfer_group_id]
    legs: dict[str, list[models.Transaction]] = {g: [] for g in group_ids}
    if group_ids:
        for t in db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == user.household_id,
                models.Transaction.transfer_group_id.in_(group_ids),
            )
        ).scalars():
            legs[t.transfer_group_id or ""].append(t)
    cats, accts = _cat_names(db, user.household_id), _acct_names(db, user.household_id)
    views = (_group_view(g, legs[g], cats, accts) for g in group_ids)
    return [v for v in views if v is not None]


@router.post("/{group_id}/unlink", response_model=schemas.CountOut)
def unlink(
    group_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.CountOut:
    """Not a transfer after all: both sides become ordinary transactions again, and the
    pair is never linked again."""
    dates = [t.txn_date for t in transfers.group_legs(db, user.household_id, group_id)]
    released = transfers.unlink(db, user.household_id, group_id)
    if not released:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    db.commit()
    _resettle(db, user.household_id, *dates)
    return schemas.CountOut(updated=released)


@router.post("/{group_id}/kind", response_model=schemas.TransferGroupOut)
def change_kind(
    group_id: str,
    payload: schemas.TransferKindIn,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.TransferGroupOut:
    pair = transfers.outflow_and_inflow(transfers.group_legs(db, user.household_id, group_id))
    if pair is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transfer not found")
    transfers.apply_kind(db, pair[0], pair[1], payload.kind)
    db.commit()
    return _group_out(db, user.household_id, group_id)
