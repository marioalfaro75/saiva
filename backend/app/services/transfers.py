"""Internal transfer detection: match equal/opposite movements between the
household's own accounts and exclude them from income/expense (PRD R14)."""

from __future__ import annotations

from collections import defaultdict
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..constants import TRANSFER_CATEGORY


def detect_transfers(db: Session, household_id: str, window_days: int = 3) -> int:
    txns = (
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household_id,
                models.Transaction.is_transfer.is_(False),
                models.Transaction.split_parent_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    transfer_category_id = db.execute(
        select(models.Category.id).where(
            models.Category.household_id == household_id,
            models.Category.name == TRANSFER_CATEGORY,
        )
    ).scalar_one_or_none()

    by_amount: dict[int, list[models.Transaction]] = defaultdict(list)
    for t in txns:
        by_amount[abs(t.amount_cents)].append(t)

    used: set[str] = set()
    linked = 0
    for amount, group in by_amount.items():
        if amount == 0:
            continue
        outflows = [t for t in group if t.amount_cents < 0]
        inflows = [t for t in group if t.amount_cents > 0]
        for out in outflows:
            if out.id in used:
                continue
            for inflow in inflows:
                if inflow.id in used or inflow.account_id == out.account_id:
                    continue
                if abs((inflow.txn_date - out.txn_date).days) <= window_days:
                    group_id = uuid4().hex
                    for t in (out, inflow):
                        t.is_transfer = True
                        t.transfer_group_id = group_id
                        if transfer_category_id:
                            t.category_id = transfer_category_id
                    used.update({out.id, inflow.id})
                    linked += 2
                    break

    db.commit()
    return linked
