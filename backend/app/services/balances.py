"""Account balances as the household states them (0.19.0).

An account's balance is its opening balance plus its transactions. When the household
says what the balance was on a day ("I owe $452,000 today"), that is kept as the
account's anchor and the opening balance is set so the account reads it then.
Transactions dated before that day that arrive later -- an older statement imported
afterwards, say -- were already in the figure the household gave, so the opening balance
is set again to keep it: they don't move the stated balance. On the day itself, a
statement's rows were in it too; one typed in by hand after the figure was given is new.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .. import models

# The opening balance is a 32-bit integer on Postgres.
MAX_OPENING_CENTS = 2**31 - 1


class BalanceTooLarge(ValueError):
    """The opening balance that would make the account read the figure doesn't fit."""


def _moved(
    db: Session, account_id: str, day: dt.date | None, stated_at: dt.datetime | None = None
) -> int:
    """What the transactions to the end of `day` add up to. With `stated_at`, a row on
    `day` typed in by hand after then is left out: it came after the figure."""
    t = models.Transaction
    q = select(func.coalesce(func.sum(t.amount_cents), 0)).where(
        t.account_id == account_id, t.split_parent_id.is_(None)
    )
    if day is not None and stated_at is None:
        q = q.where(t.txn_date <= day)
    elif day is not None:
        q = q.where(
            or_(
                t.txn_date < day,
                and_(t.txn_date == day, or_(t.source != "manual", t.created_at <= stated_at)),
            )
        )
    return int(db.execute(q).scalar() or 0)


def _opening(account: models.Account, moved: int) -> int | None:
    if account.balance_anchor_cents is None:
        return None
    opening = account.balance_anchor_cents - moved
    return opening if abs(opening) <= MAX_OPENING_CENTS else None


def balance_through(db: Session, account: models.Account, day: dt.date | None) -> int:
    """The account's balance at the end of a day (with every transaction, without one)."""
    return account.opening_balance_cents + _moved(db, account.id, day)


def set_balance(db: Session, account: models.Account, cents: int, on: dt.date) -> None:
    """The household says the account reads `cents` on `on`. Does not commit; flushes
    first, so transactions just added count. Raises BalanceTooLarge, changing nothing,
    when the opening balance it needs doesn't fit."""
    db.flush()
    stated_at = dt.datetime.utcnow()
    opening = cents - _moved(db, account.id, on, stated_at)
    if abs(opening) > MAX_OPENING_CENTS:
        raise BalanceTooLarge("That balance is too large for Saiva to hold.")
    account.balance_anchor_cents = cents
    account.balance_anchor_on = on
    account.balance_anchor_at = stated_at
    account.opening_balance_cents = opening


def clear_anchor(account: models.Account) -> None:
    """The household set the opening balance itself: that is what it says now."""
    account.balance_anchor_cents = None
    account.balance_anchor_on = None
    account.balance_anchor_at = None


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
        if account.balance_anchor_on is None:
            continue
        moved = _moved(db, account.id, account.balance_anchor_on, account.balance_anchor_at)
        opening = _opening(account, moved)
        if opening is not None:  # else left as it is: it can't hold the figure
            account.opening_balance_cents = opening
