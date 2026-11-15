"""Account balances as the household states them (0.19.0).

An account's balance is its opening balance plus its transactions. When the household
says what the balance was at the end of a day ("I owe $452,000 today"), that is kept as
the account's anchor and the opening balance is set so the account reads it on that day.
Transactions dated on or before that day that arrive later -- an older statement
imported afterwards, say -- were already in the figure the household gave, so the
opening balance is set again to keep it: they don't move the stated balance.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models


def _moved(db: Session, account_id: str, day: dt.date | None) -> int:
    q = select(func.coalesce(func.sum(models.Transaction.amount_cents), 0)).where(
        models.Transaction.account_id == account_id,
        models.Transaction.split_parent_id.is_(None),
    )
    if day is not None:
        q = q.where(models.Transaction.txn_date <= day)
    return int(db.execute(q).scalar() or 0)


def balance_through(db: Session, account: models.Account, day: dt.date | None) -> int:
    """The account's balance at the end of a day (with every transaction, without one)."""
    return account.opening_balance_cents + _moved(db, account.id, day)


def set_balance(db: Session, account: models.Account, cents: int, on: dt.date) -> None:
    """The household says the account read `cents` at the end of `on`. Does not commit;
    flushes first, so transactions just added count."""
    db.flush()
    account.balance_anchor_cents = cents
    account.balance_anchor_on = on
    account.opening_balance_cents = cents - _moved(db, account.id, on)


def clear_anchor(account: models.Account) -> None:
    """The household set the opening balance itself: that is what it says now."""
    account.balance_anchor_cents = None
    account.balance_anchor_on = None


def reanchor(db: Session, account_ids: Iterable[str]) -> None:
    """After transactions are added or removed: keep each anchored account reading what
    the household said on its day. Does not commit."""
    ids = sorted(set(account_ids))
    if not ids:
        return
    db.flush()
    for account in db.execute(
        select(models.Account).where(
            models.Account.id.in_(ids), models.Account.balance_anchor_on.is_not(None)
        )
    ).scalars():
        if account.balance_anchor_cents is None or account.balance_anchor_on is None:
            continue
        account.opening_balance_cents = account.balance_anchor_cents - _moved(
            db, account.id, account.balance_anchor_on
        )
