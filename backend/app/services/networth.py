"""Net worth (PRD Phase 2): a simple manual balance sheet of assets and
liabilities with a daily snapshot history for the trend chart. Net worth is
assets minus liabilities; account balances are intentionally not folded in, so
v1 stays manual and unambiguous."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import NetWorthItemOut, NetWorthOut, NetWorthPoint


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


def _totals(items: list[models.NetWorthItem]) -> tuple[int, int, int]:
    assets = sum(i.value_cents for i in items if i.kind == "asset")
    liabilities = sum(i.value_cents for i in items if i.kind == "liability")
    return assets, liabilities, assets - liabilities


def record_snapshot(
    db: Session, household_id: str, as_of: dt.date | None = None
) -> models.NetWorthSnapshot:
    """Upsert the net-worth total for a day (today by default) from current items."""
    as_of = as_of or dt.date.today()
    assets, liabilities, net = _totals(_items(db, household_id))
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


def get_net_worth(db: Session, household_id: str) -> NetWorthOut:
    items = _items(db, household_id)
    assets, liabilities, net = _totals(items)
    snapshots = (
        db.execute(
            select(models.NetWorthSnapshot)
            .where(models.NetWorthSnapshot.household_id == household_id)
            .order_by(models.NetWorthSnapshot.as_of)
        )
        .scalars()
        .all()
    )
    return NetWorthOut(
        assets_cents=assets,
        liabilities_cents=liabilities,
        net_cents=net,
        items=[NetWorthItemOut.model_validate(i) for i in items],
        history=[
            NetWorthPoint(
                as_of=s.as_of,
                assets_cents=s.assets_cents,
                liabilities_cents=s.liabilities_cents,
                net_cents=s.net_cents,
            )
            for s in snapshots
        ],
    )
