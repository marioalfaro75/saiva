"""Net worth (PRD Phase 2): a balance sheet of assets and liabilities with a daily
snapshot history for the trend chart. Net worth is assets minus liabilities.

The household enters items by hand (the house, a car), and chooses which accounts count
(0.19.0): an account's balance counts once the household says it is right -- in credit
an asset, owing a liability. Setting up the Mortgage page counts the loan and its
offsets."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import NetWorthAccountOut, NetWorthItemOut, NetWorthOut, NetWorthPoint


def _items(db: Session, household_id: str) -> list[models.NetWorthItem]:
    return list(
        db.execute(
            select(models.NetWorthItem)
            .where(models.NetWorthItem.household_id == household_id)
            .order_by(
                models.NetWorthItem.kind,
                models.NetWorthItem.sort,
                models.NetWorthItem.name,
            )
        )
        .scalars()
        .all()
    )


def _accounts(db: Session, household_id: str) -> list[NetWorthAccountOut]:
    """Every account with its balance today, and whether it counts."""
    accounts = (
        db.execute(
            select(models.Account)
            .where(models.Account.household_id == household_id)
            .order_by(models.Account.name)
        )
        .scalars()
        .all()
    )
    moved = dict(
        db.execute(
            select(models.Transaction.account_id, func.sum(models.Transaction.amount_cents))
            .where(
                models.Transaction.household_id == household_id,
                models.Transaction.split_parent_id.is_(None),
            )
            .group_by(models.Transaction.account_id)
        ).tuples().all()
    )
    return [
        NetWorthAccountOut(
            id=a.id,
            name=a.name,
            type=a.type,
            balance_cents=a.opening_balance_cents + int(moved.get(a.id) or 0),
            in_net_worth=bool(a.in_net_worth),
        )
        for a in accounts
    ]


def _totals(
    items: list[models.NetWorthItem], accounts: list[NetWorthAccountOut]
) -> tuple[int, int, int]:
    counted = [a.balance_cents for a in accounts if a.in_net_worth]
    assets = sum(i.value_cents for i in items if i.kind == "asset")
    assets += sum(b for b in counted if b > 0)
    liabilities = sum(i.value_cents for i in items if i.kind == "liability")
    liabilities += sum(-b for b in counted if b < 0)
    return assets, liabilities, assets - liabilities


# Snapshot totals are 32-bit integers on Postgres.
MAX_SNAPSHOT_CENTS = 2**31 - 1


def record_snapshot(
    db: Session, household_id: str, as_of: dt.date | None = None
) -> models.NetWorthSnapshot | None:
    """Upsert the net-worth total for a day (today by default) from current items and
    counted accounts. None when the totals are too large to store."""
    as_of = as_of or dt.date.today()
    assets, liabilities, net = _totals(_items(db, household_id), _accounts(db, household_id))
    if max(abs(assets), abs(liabilities), abs(net)) > MAX_SNAPSHOT_CENTS:
        return None  # beyond what a snapshot holds: today's figures are still shown live
    snapshot = db.execute(
        select(models.NetWorthSnapshot).where(
            models.NetWorthSnapshot.household_id == household_id,
            models.NetWorthSnapshot.as_of == as_of,
        )
    ).scalar_one_or_none()
    if snapshot is None:
        snapshot = models.NetWorthSnapshot(household_id=household_id, as_of=as_of)
        db.add(snapshot)
    snapshot.assets_cents = assets
    snapshot.liabilities_cents = liabilities
    snapshot.net_cents = net
    db.commit()
    return snapshot


def record_snapshot_if_used(db: Session, household_id: str) -> None:
    """Today's snapshot, when the household keeps a net worth: an item, an account that
    counts, or a history already (so taking the last account out is recorded too).
    Commits."""
    used = db.execute(
        select(models.NetWorthSnapshot.id)
        .where(models.NetWorthSnapshot.household_id == household_id)
        .limit(1)
    ).first() or db.execute(
        select(models.NetWorthItem.id).where(models.NetWorthItem.household_id == household_id)
        .limit(1)
    ).first() or db.execute(
        select(models.Account.id).where(
            models.Account.household_id == household_id,
            models.Account.in_net_worth.is_(True),
        ).limit(1)
    ).first()
    if used:
        record_snapshot(db, household_id)


def get_net_worth(
    db: Session, household_id: str, as_at: dt.date | None = None
) -> NetWorthOut:
    """The balance sheet, optionally as it stood at `as_at`.

    Items hold current balances only, so for a date in the past the totals come from
    the last snapshot on or before it and the history stops there. Without a snapshot
    that far back the current figures are the best available, and are returned as-is.
    """
    items = _items(db, household_id)
    accounts = _accounts(db, household_id)
    assets, liabilities, net = _totals(items, accounts)
    conditions = [models.NetWorthSnapshot.household_id == household_id]
    historical = as_at is not None and as_at < dt.date.today()
    if historical:
        conditions.append(models.NetWorthSnapshot.as_of <= as_at)
    snapshots = (
        db.execute(
            select(models.NetWorthSnapshot)
            .where(*conditions)
            .order_by(models.NetWorthSnapshot.as_of)
        )
        .scalars()
        .all()
    )
    if historical and snapshots:
        latest = snapshots[-1]
        assets, liabilities, net = latest.assets_cents, latest.liabilities_cents, latest.net_cents
    history = [
        NetWorthPoint(
            as_of=s.as_of,
            assets_cents=s.assets_cents,
            liabilities_cents=s.liabilities_cents,
            net_cents=s.net_cents,
        )
        for s in snapshots
    ]
    if not historical and history:
        # Today's point is today's figures: balances move with every transaction, and a
        # snapshot is only written when something on this page or an import changes.
        today = NetWorthPoint(
            as_of=dt.date.today(),
            assets_cents=assets,
            liabilities_cents=liabilities,
            net_cents=net,
        )
        if history[-1].as_of >= today.as_of:
            history[-1] = today.model_copy(update={"as_of": history[-1].as_of})
        else:
            history.append(today)
    return NetWorthOut(
        assets_cents=assets,
        liabilities_cents=liabilities,
        net_cents=net,
        items=[NetWorthItemOut.model_validate(i) for i in items],
        history=history,
        accounts=accounts,
    )
