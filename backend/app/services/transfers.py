"""Internal transfer detection: match equal/opposite movements between the
household's own accounts and exclude them from income/expense (PRD R14)."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from difflib import SequenceMatcher
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..constants import TRANSFER_CATEGORY

# Words a bank actually uses when money moves between your own accounts. Matching one
# of these is the corroborating evidence a pair needs; equal amounts alone are not.
TRANSFER_WORDS = (
    "transfer", "tfr", "xfer", "trf",
    "internal", "own account", "between accounts",
    "to savings", "from savings", "to acct", "from acct",
    "netbank", "osko", "pay anyone",
)

# How alike two descriptions must read to stand in for that vocabulary — banks
# sometimes label both legs identically and use no transfer word at all.
DESCRIPTION_SIMILARITY = 0.6


def _text(t: models.Transaction) -> str:
    return f"{t.raw_description or ''} {t.merchant or ''}".lower().strip()


def looks_like_a_transfer(out: models.Transaction, inflow: models.Transaction) -> bool:
    """Whether anything beyond the amount suggests these two are the same money.

    Equal amounts, different accounts and a few days apart describes an enormous number
    of ordinary pairs — a rent payment and a salary, a bill and a refund. Linking on
    that alone marked both legs as internal, removed them from every total the app
    reports, and did it silently over the household's whole history after each import.
    """
    a, b = _text(out), _text(inflow)
    if any(word in a or word in b for word in TRANSFER_WORDS):
        return True
    return SequenceMatcher(None, a, b).ratio() >= DESCRIPTION_SIMILARITY


def candidates(
    inflows_by_day: dict[tuple[int, dt.date], list[models.Transaction]],
    out: models.Transaction,
    window_days: int,
) -> list[models.Transaction]:
    """Inflows that could possibly pair with this outflow.

    Only the days inside the window, rather than every inflow that happens to share
    an amount. Grouping on amount alone meant 600 outflows each walked 600 inflows
    to reach the twenty or so in range, and a file crafted with tens of thousands of
    identical amounts turned that into an import that never finished.
    """
    amount = abs(out.amount_cents)
    found: list[models.Transaction] = []
    for offset in range(-window_days, window_days + 1):
        found.extend(inflows_by_day.get((amount, out.txn_date + dt.timedelta(days=offset)), ()))
    return found


def detect_transfers(
    db: Session,
    household_id: str,
    window_days: int = 3,
    *,
    since: dt.date | None = None,
) -> int:
    conditions = [
        models.Transaction.household_id == household_id,
        models.Transaction.is_transfer.is_(False),
        models.Transaction.split_parent_id.is_(None),
    ]
    if since is not None:
        # Scoped to what an import actually touched, rather than re-deciding the whole
        # history every time. It is also what stops a crafted row reaching back years.
        conditions.append(models.Transaction.txn_date >= since - dt.timedelta(days=window_days))
    txns = db.execute(select(models.Transaction).where(*conditions)).scalars().all()
    transfer_category_id = db.execute(
        select(models.Category.id).where(
            models.Category.household_id == household_id,
            models.Category.name == TRANSFER_CATEGORY,
        )
    ).scalar_one_or_none()

    # Bucketed on (amount, date) rather than amount alone. Only a few days either
    # side can ever pair, so scanning every same-amount inflow for every outflow did
    # quadratic work for no extra matches — and an import of tens of thousands of
    # identical amounts turned that into a stall anyone could trigger with a file.
    outflows: list[models.Transaction] = []
    inflows_by_day: dict[tuple[int, dt.date], list[models.Transaction]] = defaultdict(list)
    for t in txns:
        amount = abs(t.amount_cents)
        if amount == 0:
            continue
        if t.amount_cents < 0:
            outflows.append(t)
        else:
            inflows_by_day[(amount, t.txn_date)].append(t)

    used: set[str] = set()
    linked = 0
    for out in outflows:
        if out.id in used:
            continue
        for inflow in candidates(inflows_by_day, out, window_days):
            if inflow.id in used or inflow.account_id == out.account_id:
                continue
            if not looks_like_a_transfer(out, inflow):
                continue
            group_id = uuid4().hex
            for t in (out, inflow):
                t.is_transfer = True
                t.transfer_group_id = group_id
                # A locked category is a decision the user made explicitly.
                # categorise.py already refuses to overwrite one; this path did not,
                # so a category someone had pinned was replaced without a word.
                if transfer_category_id and not t.category_locked:
                    t.category_id = transfer_category_id
            used.update({out.id, inflow.id})
            linked += 2
            break

    db.commit()
    return linked
