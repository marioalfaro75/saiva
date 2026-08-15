"""Duplicate detection for statement imports (PRD R6).

Three tiers, most reliable first:

1. **Provider id** — the bank's own `FITID` (OFX/QFX). Definitive, and the only
   signal that survives the bank re-dating *and* re-wording a transaction.
2. **Exact match** on account + date + amount + description, matched by
   *multiplicity* rather than membership (see `DuplicateMatcher`).
3. **Near match** — same amount, date within a few days, similar wording. Catches
   pending-vs-posted re-dating and descriptions that pick up a reference number
   between exports. Reported as *probable* so a human confirms it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .importers import ParsedTxn, dedup_hash

# Banks re-date transactions between exports (pending -> posted, authorisation vs
# settlement), so a near-match this far either side of the incoming date counts.
DATE_WINDOW_DAYS = 3
# Similarity required between normalised descriptions for a near match. The amount
# is already identical and the date within the window by this point, so this only
# has to establish "plausibly the same merchant".
SIMILARITY_THRESHOLD = 0.82

NEW = "new"
DUPLICATE_PROVIDER = "duplicate_provider"
DUPLICATE_EXACT = "duplicate_exact"
DUPLICATE_PROBABLE = "duplicate_probable"

# Card suffixes, receipt and reference numbers — these commonly differ between two
# exports of the same transaction, so they are stripped before comparing.
_REFERENCE_NOISE = re.compile(r"\b(?:x{2,}\d*|\*+\d*|\d{4,})\b")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def fuzzy_norm(description: str) -> str:
    """Normalise a description for near-match comparison."""
    s = (description or "").lower()
    s = _REFERENCE_NOISE.sub(" ", s)
    s = _NON_ALNUM.sub(" ", s)
    return _SPACES.sub(" ", s).strip()


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class Candidate:
    """A stored transaction that an incoming row might duplicate."""

    id: str
    txn_date: dt.date
    amount_cents: int
    raw_description: str
    dedup_hash: str
    provider_txn_id: str | None
    consumed: bool = False


@dataclass
class Verdict:
    status: str
    reason: str | None = None
    matched: Candidate | None = None

    @property
    def is_duplicate(self) -> bool:
        return self.status != NEW


def load_candidates(db: Session, account_id: str) -> list[Candidate]:
    rows = db.execute(
        select(
            models.Transaction.id,
            models.Transaction.txn_date,
            models.Transaction.amount_cents,
            models.Transaction.raw_description,
            models.Transaction.dedup_hash,
            models.Transaction.provider_txn_id,
        ).where(models.Transaction.account_id == account_id)
    ).all()
    return [
        Candidate(
            id=r[0],
            txn_date=r[1],
            amount_cents=r[2],
            raw_description=r[3] or "",
            dedup_hash=r[4] or "",
            provider_txn_id=r[5],
        )
        for r in rows
    ]


class DuplicateMatcher:
    """Matches incoming rows against one account's stored transactions.

    Each stored row can be consumed by at most one incoming row. That multiplicity
    check is what lets genuine same-day repeats survive: a statement listing two
    identical $4.50 coffees records two real events, and treating the second as a
    duplicate of the first would silently lose it. Rows within a file are therefore
    never matched against each other — only against what is already stored. Importing
    the same file twice still skips everything, because the second run finds two
    stored rows for its two incoming rows to consume.
    """

    def __init__(self, existing: list[Candidate]) -> None:
        self._by_provider: dict[str, list[Candidate]] = {}
        self._by_hash: dict[str, list[Candidate]] = {}
        self._by_amount: dict[int, list[Candidate]] = {}
        for c in existing:
            if c.provider_txn_id:
                self._by_provider.setdefault(c.provider_txn_id, []).append(c)
            self._by_hash.setdefault(c.dedup_hash, []).append(c)
            self._by_amount.setdefault(c.amount_cents, []).append(c)

    @staticmethod
    def _take(bucket: list[Candidate] | None) -> Candidate | None:
        for c in bucket or ():
            if not c.consumed:
                return c
        return None

    def match(self, account_id: str, p: ParsedTxn) -> Verdict:
        if p.provider_txn_id:
            m = self._take(self._by_provider.get(p.provider_txn_id))
            if m:
                m.consumed = True
                return Verdict(DUPLICATE_PROVIDER, "Same bank transaction id", m)

        h = dedup_hash(account_id, p.txn_date, p.amount_cents, p.raw_description)
        m = self._take(self._by_hash.get(h))
        if m:
            m.consumed = True
            return Verdict(DUPLICATE_EXACT, "Already imported", m)

        best: Candidate | None = None
        best_score = 0.0
        norm = fuzzy_norm(p.raw_description)
        for c in self._by_amount.get(p.amount_cents, ()):
            if c.consumed or abs((c.txn_date - p.txn_date).days) > DATE_WINDOW_DAYS:
                continue
            score = similarity(norm, fuzzy_norm(c.raw_description))
            if score > best_score:
                best, best_score = c, score
        if best is not None and best_score >= SIMILARITY_THRESHOLD:
            best.consumed = True
            days = abs((best.txn_date - p.txn_date).days)
            when = "same day" if days == 0 else f"{days} day{'s' if days > 1 else ''} apart"
            return Verdict(DUPLICATE_PROBABLE, f"Similar transaction {when}", best)

        return Verdict(NEW)


class Deduper:
    """Duplicate matching across one import run, which may span several accounts.

    Candidates are loaded per account on first use and the matcher is kept, so
    consumption is tracked correctly for the whole run.
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._matchers: dict[str, DuplicateMatcher] = {}

    def match(self, account_id: str, p: ParsedTxn) -> Verdict:
        matcher = self._matchers.get(account_id)
        if matcher is None:
            matcher = DuplicateMatcher(load_candidates(self._db, account_id))
            self._matchers[account_id] = matcher
        return matcher.match(account_id, p)
