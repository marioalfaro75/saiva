"""Duplicate detection for statement imports (PRD R6).

Three tiers, most reliable first:

1. **Provider id** — the bank's own `FITID` (OFX/QFX). Definitive, and the only
   signal that survives the bank re-dating *and* re-wording a transaction.
2. **Exact match** on account + date + amount + description, matched by
   *multiplicity* rather than membership (see `DuplicateMatcher`).
3. **Near match** — same amount, date within a few days, similar wording. Catches
   pending-vs-posted re-dating and descriptions that pick up a reference number
   between exports. Reported as *probable* so a human confirms it.
4. **Same merchant** — same amount and day, same normalised merchant, wording
   unalike. Catches one purchase worded two ways in two export formats. Also
   *probable*.

A `Verdict` carries the stored row it matched so the preview can show it. Tiers 3
and 4 ask a person to decide, and a person cannot decide from a status alone — so
`Candidate` holds enough of the stored transaction to be shown next to the incoming
one, and `tier` says which of the four found it, because the four need different
explanations. Note that tiers 3 and 4 both reach the candidate through `_window()`,
which buckets on `(amount, date)`: on a probable duplicate the amount is therefore
*identical by construction*, and the fields that actually separate one purchase from
two are the wording and the direction of the date gap.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
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

# Backstop for the near-match scan. Bucketing by (amount, date) already keeps this
# small for real statements; a file crafted to put tens of thousands of rows in one
# bucket stops being matched exhaustively rather than stalling the import. Giving up
# early can only report a row as new, never as a duplicate it is not.
MAX_NEAR_MATCH_CANDIDATES = 500

NEW = "new"
DUPLICATE_PROVIDER = "duplicate_provider"
DUPLICATE_EXACT = "duplicate_exact"
DUPLICATE_PROBABLE = "duplicate_probable"

# Which tier found the match. `status` collapses tiers 3 and 4 into one value, which
# is right for deciding whether to import and wrong for explaining why — so the tier
# is reported alongside it rather than inferred from the reason string.
TIER_PROVIDER = "provider_id"
TIER_EXACT = "exact_hash"
TIER_NEAR = "near_match"
TIER_MERCHANT = "same_merchant"

# How far either side of the incoming date to look when counting what else the
# account already holds nearby. Wider than DATE_WINDOW_DAYS on purpose: the matcher
# only pairs within a few days, but "is this a weekly repeat?" needs a fortnight to
# answer. Only ever applied to rows already in memory — see `neighbours()`.
NEIGHBOURHOOD_DAYS = 7
# Shown per row. The list exists to establish cardinality, not to be browsed.
MAX_NEIGHBOURS_SHOWN = 3

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


# How two descriptions relate. Four values rather than three on purpose: collapsing
# the last two into one "differs only in a reference number" over-claims, because
# `fuzzy_norm` strips any run of four or more digits — so "TRANSFER TO 123456789" and
# "TRANSFER TO 987654321" normalise equal while being payments to different payees,
# which is exactly what rent and a loan repayment look like. WORDING_DIGITS keeps that
# case separate so the reader is told the digits differ instead of being reassured.
WORDING_SAME = "same"
WORDING_NOISE = "noise"
WORDING_DIGITS = "digits"
WORDING_DIFFERS = "differs"

_DIGIT_RUNS = re.compile(r"\d{4,}")


def wording_relation(a: str, b: str) -> str:
    """Whether two descriptions say the same thing, and if not, how they differ."""
    left, right = (a or "").strip(), (b or "").strip()
    if _SPACES.sub(" ", left.lower()) == _SPACES.sub(" ", right.lower()):
        return WORDING_SAME
    if fuzzy_norm(left) != fuzzy_norm(right):
        return WORDING_DIFFERS
    # Equal once reference noise is stripped. Whether that is reassuring depends
    # entirely on whether the stripped numbers were the same number.
    if _DIGIT_RUNS.findall(left) == _DIGIT_RUNS.findall(right):
        return WORDING_NOISE
    return WORDING_DIGITS


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class Candidate:
    """A stored transaction that an incoming row might duplicate.

    Everything after `merchant` exists so the preview can show this row to a person
    rather than merely assert that it exists. All of it is defaulted: tests construct
    a Candidate positionally with the matching fields only, and a required field here
    would break them for no reason.
    """

    id: str
    txn_date: dt.date
    amount_cents: int
    raw_description: str
    dedup_hash: str
    provider_txn_id: str | None
    merchant: str = ""
    category_id: str | None = None
    # "import" or "manual". A hand-entered row is the case where importing a second
    # copy matters most, and the only one the app could not previously express.
    source: str = "import"
    import_batch_id: str | None = None
    created_at: dt.datetime | None = None
    consumed: bool = False
    _norm: str | None = None

    @property
    def norm(self) -> str:
        """Normalised description, computed once.

        It used to be recomputed for every incoming row that reached the near-match
        tier, so importing n rows against n stored rows of the same amount ran the
        regex pipeline n² times before SequenceMatcher had even started.
        """
        if self._norm is None:
            self._norm = fuzzy_norm(self.raw_description)
        return self._norm


@dataclass
class Neighbour:
    """Another stored row near the match, for establishing how many you already have.

    One candidate shown in isolation answers "does something like this exist" and not
    "how many of these do I have" — and the second is the question that separates a
    re-dated duplicate from a weekly repeat. `same_amount` and `same_merchant` record
    *why* this row is being shown, because the copy must not claim to have searched
    more than it did.
    """

    id: str
    txn_date: dt.date
    amount_cents: int
    raw_description: str
    merchant: str
    same_amount: bool
    same_merchant: bool


@dataclass
class Verdict:
    status: str
    reason: str | None = None
    matched: Candidate | None = None
    # One of the TIER_* constants. `status` cannot distinguish the two probable
    # tiers, and they need different explanations.
    tier: str | None = None

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
            models.Transaction.merchant,
            models.Transaction.category_id,
            models.Transaction.source,
            models.Transaction.import_batch_id,
            models.Transaction.created_at,
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
            merchant=r[6] or "",
            category_id=r[7],
            source=r[8] or "import",
            import_batch_id=r[9],
            created_at=r[10],
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
        # Keyed on (amount, date), not amount alone. The near-match tier only ever
        # considers candidates inside the date window, so bucketing on amount alone
        # meant walking every stored transaction that shared an amount — a file of
        # 50,000 identical $4.50 rows against a history of the same made the import
        # quadratic, which is a denial of service anyone with an upload form can run.
        self._by_amount_and_date: dict[tuple[int, dt.date], list[Candidate]] = {}
        # Same reasoning as above, for the neighbourhood lookup: a frequent merchant
        # over a fortnight is a handful of rows, but finding them by walking the
        # account's whole history once per review row is the same quadratic shape.
        self._by_merchant_and_date: dict[tuple[str, dt.date], list[Candidate]] = {}
        for c in existing:
            if c.provider_txn_id:
                self._by_provider.setdefault(c.provider_txn_id, []).append(c)
            self._by_hash.setdefault(c.dedup_hash, []).append(c)
            self._by_amount_and_date.setdefault((c.amount_cents, c.txn_date), []).append(c)
            if c.merchant:
                self._by_merchant_and_date.setdefault((c.merchant, c.txn_date), []).append(c)

    def _window(self, p: ParsedTxn) -> Iterator[Candidate]:
        """Unconsumed candidates with this amount, within the date window."""
        seen = 0
        for offset in range(-DATE_WINDOW_DAYS, DATE_WINDOW_DAYS + 1):
            day = p.txn_date + dt.timedelta(days=offset)
            for c in self._by_amount_and_date.get((p.amount_cents, day), ()):
                if c.consumed:
                    continue
                seen += 1
                if seen > MAX_NEAR_MATCH_CANDIDATES:
                    return
                yield c

    def neighbours(
        self, p: ParsedTxn, exclude_id: str | None = None
    ) -> tuple[list[Neighbour], int]:
        """Stored rows near this date at the same amount or merchant.

        Returns the first few and the total found. Two dict lookups per day of the
        window rather than a scan of the account: the candidates are already in
        memory, so this costs no query, but reaching them by filtering the whole
        history once per review row would reintroduce exactly the quadratic blow-up
        `_window` exists to avoid.

        `consumed` is deliberately ignored. A row claimed by an earlier incoming row
        is still a row the household already has, and the count is the point.
        """
        found: dict[str, Neighbour] = {}
        for offset in range(-NEIGHBOURHOOD_DAYS, NEIGHBOURHOOD_DAYS + 1):
            day = p.txn_date + dt.timedelta(days=offset)
            by_amount = self._by_amount_and_date.get((p.amount_cents, day), ())
            by_merchant = (
                self._by_merchant_and_date.get((p.merchant, day), ()) if p.merchant else ()
            )
            for c, same_amount in [(c, True) for c in by_amount] + [
                (c, False) for c in by_merchant
            ]:
                if c.id == exclude_id:
                    continue
                existing = found.get(c.id)
                if existing is not None:
                    # Seen through the other index too — record both reasons.
                    existing.same_amount = existing.same_amount or same_amount
                    existing.same_merchant = existing.same_merchant or not same_amount
                    continue
                found[c.id] = Neighbour(
                    id=c.id,
                    txn_date=c.txn_date,
                    amount_cents=c.amount_cents,
                    raw_description=c.raw_description,
                    merchant=c.merchant,
                    same_amount=same_amount,
                    same_merchant=not same_amount,
                )
        ordered = sorted(found.values(), key=lambda n: (n.txn_date, n.id))
        return ordered[:MAX_NEIGHBOURS_SHOWN], len(ordered)

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
                return Verdict(DUPLICATE_PROVIDER, "Same bank transaction id", m, TIER_PROVIDER)

        h = dedup_hash(account_id, p.txn_date, p.amount_cents, p.raw_description)
        m = self._take(self._by_hash.get(h))
        if m:
            m.consumed = True
            return Verdict(DUPLICATE_EXACT, "Already imported", m, TIER_EXACT)

        best: Candidate | None = None
        best_score = 0.0
        same_merchant: Candidate | None = None
        norm = fuzzy_norm(p.raw_description)
        for c in self._window(p):
            score = similarity(norm, c.norm)
            if score > best_score:
                best, best_score = c, score
            if (
                same_merchant is None
                and p.merchant
                and c.merchant
                and c.merchant == p.merchant
                and c.txn_date == p.txn_date
            ):
                same_merchant = c

        weak_similarity = best is None or best_score < SIMILARITY_THRESHOLD
        if weak_similarity and same_merchant is not None:
            # A bank words the same transaction differently in different exports —
            # "WOOLWORTHS METRO SYDNEY" in OFX, "EFTPOS WOOLWORTHS 4521 SYDNEY NSW"
            # in CSV. Those score far below the similarity threshold, so importing
            # both formats of one month produced two rows for every transaction.
            # Normalised merchant is the signal that survives the rewording, and it
            # still separates two different shops that happen to charge the same
            # amount on the same day. Same date only, not the whole window: this is
            # the high-confidence subset, and the verdict is `probable` either way,
            # so a human confirms before anything is skipped.
            same_merchant.consumed = True
            return Verdict(
                DUPLICATE_PROBABLE,
                f"Same merchant and amount on {same_merchant.txn_date:%d %b %Y}, "
                "worded differently",
                same_merchant,
                TIER_MERCHANT,
            )
        if best is not None and best_score >= SIMILARITY_THRESHOLD:
            best.consumed = True
            days = abs((best.txn_date - p.txn_date).days)
            when = "same day" if days == 0 else f"{days} day{'s' if days > 1 else ''} apart"
            return Verdict(DUPLICATE_PROBABLE, f"Similar transaction {when}", best, TIER_NEAR)

        return Verdict(NEW)


class Deduper:
    """Duplicate matching across one import run, which may span several accounts.

    Candidates are loaded per account on first use and the matcher is kept, so
    consumption is tracked correctly for the whole run.
    """

    def __init__(self, db: Session) -> None:
        self._db = db
        self._matchers: dict[str, DuplicateMatcher] = {}

    def _matcher(self, account_id: str) -> DuplicateMatcher:
        matcher = self._matchers.get(account_id)
        if matcher is None:
            matcher = DuplicateMatcher(load_candidates(self._db, account_id))
            self._matchers[account_id] = matcher
        return matcher

    def match(self, account_id: str, p: ParsedTxn) -> Verdict:
        return self._matcher(account_id).match(account_id, p)

    def neighbours(
        self, account_id: str, p: ParsedTxn, exclude_id: str | None = None
    ) -> tuple[list[Neighbour], int]:
        """What else this account already holds near the row. See the matcher's own.

        Reuses the matcher already built for this account, so asking costs no query —
        which is what makes it safe to ask per review row.
        """
        return self._matcher(account_id).neighbours(p, exclude_id)
