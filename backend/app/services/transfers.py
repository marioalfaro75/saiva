"""Money moving between the household's own accounts, and what kind it is (PRD R14).

An outflow in one account and an equal inflow in another a few days apart may be one
amount of money moving, not an expense and an income. What it is depends on where it
went: into a credit card it is a card payment (the card's purchases are the spending),
into a loan a repayment (the paying side is spending, a housing cost), anywhere else
money moved between accounts (counted nowhere). A deposit into savings that came from
none of the household's accounts is a cash deposit, and counts as income.

Pairs with something that links them -- the bank's transfer wording, the household's own
wording for that account, the account's number -- are linked at once. Pairs that only
agree on amount and date are offered for review: equal amounts across two accounts in a
few days describe a rent payment and a salary as easily as a transfer.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..constants import (
    CARD_PAYMENT,
    CARD_PAYMENT_CATEGORY,
    CASH_DEPOSIT_CATEGORY,
    DEPOSIT_ACCOUNT_TYPES,
    LOAN_ACCOUNT_TYPES,
    LOAN_REPAYMENT,
    LOAN_REPAYMENT_CATEGORY,
    MORTGAGE_CATEGORY,
    OWN_ACCOUNTS,
    TRANSFER_CATEGORY,
)
from .categorise import ML_MAX_CONFIDENCE, RULE_CONFIDENCE, set_by_person

WINDOW_DAYS = 3

# What an account's wording may hold, the same limits the API puts on a person's edit.
MAX_WORDING_LENGTH = 80
MAX_WORDING_PHRASES = 30

# Words a bank actually uses when money moves between your own accounts. Matching one
# of these is the corroborating evidence a pair needs; equal amounts alone are not.
TRANSFER_WORDS = (
    "transfer", "tfr", "xfer", "trf",
    "internal", "own account", "between accounts",
    "to savings", "from savings", "to acct", "from acct",
    "netbank", "osko", "pay anyone",
)

# Words that, on the side of a pair arriving in a card or a loan, say it is a payment
# to it: a bank labels the receiving side of a card payment "Payment received, thank
# you", not with a transfer word.
ARRIVING_WORDS = {
    "credit_card": ("payment", "thank you", "bpay"),
    "home_loan": ("repayment", "payment", "transfer"),
    "personal_loan": ("repayment", "payment", "transfer"),
}

# How alike two descriptions must read to stand in for that vocabulary — banks
# sometimes label both legs identically and use no transfer word at all.
DESCRIPTION_SIMILARITY = 0.6


def _text(t: models.Transaction) -> str:
    return f"{t.raw_description or ''} {t.merchant or ''}".lower().strip()


def wording_of(text: str) -> str:
    """A description as wording is compared: lower case, no digits or punctuation, one
    space between words. "By Authority To Cc 0412" and "BY AUTHORITY TO CC" are one."""
    return " ".join(re.sub(r"[^a-z ]+", " ", text.lower()).split())


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


def kind_for_type(account_type: str) -> str:
    """What a pair is, by the type of the account the money went into."""
    if account_type == "credit_card":
        return CARD_PAYMENT
    if account_type in LOAN_ACCOUNT_TYPES:
        return LOAN_REPAYMENT
    return OWN_ACCOUNTS


def kind_for(destination: models.Account) -> str:
    """What a pair is, by the account the money went into."""
    return kind_for_type(destination.type)


def _account_digits(account: models.Account) -> str | None:
    digits = re.sub(r"\D", "", account.bank_identifier or "")
    return digits[-4:] if len(digits) >= 4 else None


def has_phrase(words: str, phrase: str) -> bool:
    """Whether `phrase` (as wording) is in `words` (as wording) as whole words: "cc" is
    in "by authority to cc" but not in "account keeping fee"."""
    key = wording_of(phrase)
    return bool(key) and f" {key} " in f" {words} "


# How strongly the evidence ties the two legs to each other. Wording for this account,
# its account number, or two descriptions that read alike tie them; a transfer word on
# both sides nearly does; a word on one side alone ("Payment received" arriving in the
# card) only says that side is a transfer, not which outflow it is the other half of.
TIED = 3
BOTH_SIDES = 2
ONE_SIDE = 1


def _strength(
    out: models.Transaction,
    inflow: models.Transaction,
    accounts: dict[str, models.Account],
) -> tuple[int, str] | None:
    source, destination = accounts.get(out.account_id), accounts.get(inflow.account_id)
    out_words, in_words = wording_of(_text(out)), wording_of(_text(inflow))
    sides = [
        (account, words, text)
        for account, words, text in (
            (destination, out_words, out.raw_description),
            (source, in_words, inflow.raw_description),
        )
        if account is not None
    ]
    for account, words, _ in sides:
        for phrase in account.transfer_wording or []:
            if has_phrase(words, phrase):
                return TIED, f"“{phrase}” is your wording for {account.name}"
    for account, _, text in sides:
        digits = _account_digits(account)
        if digits and any(run.endswith(digits) for run in re.findall(r"\d{4,}", text or "")):
            return TIED, f"mentions {account.name}'s account number"
    a, b = _text(out), _text(inflow)
    if SequenceMatcher(None, a, b).ratio() >= DESCRIPTION_SIMILARITY:
        return TIED, "the two descriptions match"
    arriving = ARRIVING_WORDS.get(destination.type, ()) if destination is not None else ()
    out_side = any(word in a for word in TRANSFER_WORDS)
    in_side = any(word in b for word in (*TRANSFER_WORDS, *arriving))
    if out_side and in_side:
        return BOTH_SIDES, "your bank's transfer wording"
    if out_side or in_side:
        if not out_side and destination is not None and any(word in b for word in arriving):
            return ONE_SIDE, f"a payment arriving in {destination.name}"
        return ONE_SIDE, "your bank's transfer wording"
    return None


def evidence_for(
    out: models.Transaction,
    inflow: models.Transaction,
    accounts: dict[str, models.Account],
) -> str | None:
    """Why these two are one amount of money moving, in a few words -- or None when
    nothing beyond the amount and the dates says so."""
    found = _strength(out, inflow, accounts)
    return found[1] if found else None


@dataclass
class Pair:
    outflow: models.Transaction
    inflow: models.Transaction
    kind: str
    evidence: str | None  # None = only the amount and dates agree: for review

    @property
    def sure(self) -> bool:
        return self.evidence is not None


@dataclass
class TransferScan:
    linked: list[Pair] = field(default_factory=list)  # linked by this scan
    to_review: list[Pair] = field(default_factory=list)  # waiting for a person
    cash_deposits: int = 0


def _category_ids(db: Session, household_id: str) -> dict[str, str]:
    keys = (
        TRANSFER_CATEGORY,
        CARD_PAYMENT_CATEGORY,
        CASH_DEPOSIT_CATEGORY,
        MORTGAGE_CATEGORY,
        LOAN_REPAYMENT_CATEGORY,
    )
    return {
        key: cid
        for cid, key in db.execute(
            select(models.Category.id, models.Category.system_key).where(
                models.Category.household_id == household_id,
                models.Category.system_key.in_(keys),
            )
        ).tuples()
        if key
    }


def _dismissed(db: Session, household_id: str) -> set[tuple[str, str]]:
    return {
        (o, i)
        for o, i in db.execute(
            select(models.TransferDismissal.outflow_id, models.TransferDismissal.inflow_id).where(
                models.TransferDismissal.household_id == household_id
            )
        ).tuples()
    }


def _accounts(db: Session, household_id: str) -> dict[str, models.Account]:
    return {
        a.id: a
        for a in db.execute(
            select(models.Account).where(models.Account.household_id == household_id)
        ).scalars()
    }


def _income_ids(db: Session, household_id: str) -> set[str]:
    return {
        cid
        for (cid,) in db.execute(
            select(models.Category.id).where(
                models.Category.household_id == household_id, models.Category.kind == "income"
            )
        ).tuples()
    }


def _income_already(t: models.Transaction, income_ids: set[str]) -> bool:
    """An arriving amount a person or a rule already filed as income: a salary that
    happens to equal a payment out of another account is not worth asking about."""
    return t.category_id in income_ids and (set_by_person(t) or (t.confidence or 0) >= 0.99)


def _split_parent_ids(db: Session, household_id: str) -> set[str]:
    return {
        pid
        for (pid,) in db.execute(
            select(models.Transaction.split_parent_id)
            .where(
                models.Transaction.household_id == household_id,
                models.Transaction.split_parent_id.is_not(None),
            )
            .distinct()
        ).tuples()
        if pid
    }


@dataclass
class _Pairing:
    pairs: list[Pair]
    # Inflows something unlinked could still be the other half of: never a cash deposit.
    possible: set[str]


def _pairing(
    db: Session,
    household_id: str,
    *,
    since: dt.date | None,
    window_days: int,
    income_ids: set[str],
) -> _Pairing:
    conditions = [
        models.Transaction.household_id == household_id,
        models.Transaction.transfer_group_id.is_(None),
        models.Transaction.split_parent_id.is_(None),
        models.Transaction.amount_cents != 0,
    ]
    if since is not None:
        # Scoped to what an import actually touched, rather than re-deciding the whole
        # history every time. It is also what stops a crafted row reaching back years.
        conditions.append(models.Transaction.txn_date >= since - dt.timedelta(days=window_days))
    split = _split_parent_ids(db, household_id)
    rows = [
        t
        for t in db.execute(select(models.Transaction).where(*conditions)).scalars().all()
        if t.id not in split
    ]
    accounts = _accounts(db, household_id)
    dismissed = _dismissed(db, household_id)

    # Bucketed on (amount, date) rather than amount alone. Only a few days either
    # side can ever pair, so scanning every same-amount inflow for every outflow did
    # quadratic work for no extra matches — and an import of tens of thousands of
    # identical amounts turned that into a stall anyone could trigger with a file.
    outflows = sorted(
        (t for t in rows if t.amount_cents < 0), key=lambda t: (t.txn_date, t.id)
    )
    inflows_by_day: dict[tuple[int, dt.date], list[models.Transaction]] = defaultdict(list)
    for t in sorted((t for t in rows if t.amount_cents > 0), key=lambda t: (t.txn_date, t.id)):
        inflows_by_day[(t.amount_cents, t.txn_date)].append(t)

    def options(out: models.Transaction) -> list[models.Transaction]:
        return [
            i
            for i in candidates(inflows_by_day, out, window_days)
            if i.account_id != out.account_id
            and i.account_id in accounts
            and out.account_id in accounts
            and (out.id, i.id) not in dismissed
        ]

    considered = {out.id: options(out) for out in outflows}

    def days(out: models.Transaction, i: models.Transaction) -> int:
        return abs((i.txn_date - out.txn_date).days)

    Edge = tuple[
        tuple[int, int, dt.date, dt.date, str, str],
        models.Transaction,
        models.Transaction,
        str | None,
    ]
    sure: list[Edge] = []
    unsure: list[Edge] = []
    for out in outflows:
        for i in considered[out.id]:
            evidence = _strength(out, i, accounts)
            strength, why = evidence if evidence is not None else (0, None)
            key = (-strength, days(out, i), out.txn_date, i.txn_date, out.id, i.id)
            if strength > ONE_SIDE:
                sure.append((key, out, i, why))
            elif not _income_already(i, income_ids):
                # One side's wording alone says that side is a transfer, not which
                # transaction is its other half; equal amounts say less still. For a
                # person -- the likeliest pairing first.
                unsure.append((key, out, i, None))

    pairs: list[Pair] = []
    used: set[str] = set()
    for edges in (sure, unsure):
        for _, edge_out, edge_in, edge_why in sorted(edges, key=lambda e: e[0]):
            if edge_out.id in used or edge_in.id in used:
                continue
            pairs.append(
                Pair(edge_out, edge_in, kind_for(accounts[edge_in.account_id]), edge_why)
            )
            used.update({edge_out.id, edge_in.id})
    linked = {leg.id for p in pairs if p.sure for leg in (p.outflow, p.inflow)}
    # A linked inflow is never a cash deposit, so only the outflows still free count.
    possible = {i.id for out in outflows if out.id not in linked for i in considered[out.id]}
    return _Pairing(pairs, possible)


def find_pairs(
    db: Session,
    household_id: str,
    *,
    since: dt.date | None = None,
    window_days: int = WINDOW_DAYS,
) -> list[Pair]:
    """Every pair not yet linked: the ones with evidence, then the rest for review.

    Linked only on evidence that ties the two legs to each other -- the household's
    wording for the account, its number, two descriptions that read alike, or transfer
    wording on both sides -- so whether a pair is linked depends on the pair alone, not
    on what else happens to be in view. The strongest evidence is settled first, across
    all outflows. Everything else is for a person, the likeliest first. A pair a person
    dismissed is never offered again, and a split transaction is not paired (its parts
    are what count).
    """
    return _pairing(
        db,
        household_id,
        since=since,
        window_days=window_days,
        income_ids=_income_ids(db, household_id),
    ).pairs


def _may_refile(t: models.Transaction, legacy_ids: Iterable[str]) -> bool:
    """Whether linking may set this leg's category: not a locked one, nor one a person
    chose -- except Internal transfers with no score on a leg an older release linked,
    which it wrote that way, so it only reads as a person's."""
    if t.category_locked:
        return False
    return not set_by_person(t) or (t.confidence is None and t.category_id in set(legacy_ids))


def apply_kind(
    db: Session,
    outflow: models.Transaction,
    inflow: models.Transaction,
    kind: str,
    accounts: dict[str, models.Account] | None = None,
    categories: dict[str, str] | None = None,
) -> None:
    """Make the two legs one linked pair of this kind, counted the way the kind is.

    A card payment and money between accounts are counted nowhere: both legs are left
    out of spending and income. A loan repayment is spending on the paying side (the
    household's housing cost) and left out on the loan's side, where it is only the
    balance going down. A category a person chose or locked stays as it is.
    """
    household_id = outflow.household_id
    accounts = accounts if accounts is not None else _accounts(db, household_id)
    categories = categories if categories is not None else _category_ids(db, household_id)
    group = outflow.transfer_group_id or inflow.transfer_group_id or uuid4().hex
    if kind == CARD_PAYMENT:
        category = categories.get(CARD_PAYMENT_CATEGORY)
    elif kind == LOAN_REPAYMENT:
        destination = accounts.get(inflow.account_id)
        category = categories.get(
            MORTGAGE_CATEGORY
            if destination is not None and destination.type == "home_loan"
            else LOAN_REPAYMENT_CATEGORY
        )
    else:
        category = categories.get(TRANSFER_CATEGORY)
    internal = [categories[TRANSFER_CATEGORY]] if TRANSFER_CATEGORY in categories else []
    relinking = bool(outflow.transfer_group_id or inflow.transfer_group_id)
    for t in (outflow, inflow):
        legacy = internal if t.transfer_group_id else []
        t.transfer_group_id = group
        t.transfer_kind = kind
        t.is_transfer = not (kind == LOAN_REPAYMENT and t is outflow)
        if category is not None and _may_refile(t, legacy) and t.category_id != category:
            t.category_id = category
            # Automatic, not a person's: the score a row carried was for the category
            # this replaces, and a person's 1.0 for Coffee left on it would read as a
            # person's choice of Credit-card payments.
            t.confidence = ML_MAX_CONFIDENCE
    if relinking:
        # A leg split after it was linked: its parts are what the totals count, so they
        # are counted the way the kind now says. Their own categories stay theirs.
        for part in db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household_id,
                models.Transaction.split_parent_id.in_([outflow.id, inflow.id]),
            )
        ).scalars():
            part.transfer_group_id = group
            part.transfer_kind = kind
            part.is_transfer = not (kind == LOAN_REPAYMENT and part.split_parent_id == outflow.id)


def _file_cash_deposits(
    db: Session,
    household_id: str,
    rows: Iterable[models.Transaction],
    accounts: dict[str, models.Account],
    categories: dict[str, str],
    waiting: set[str],
    income_ids: set[str],
) -> int:
    """File money arriving in savings from none of the household's accounts as a cash
    deposit -- only what nothing better has placed: no category yet, or only the
    learner's guess of a spending category (money arriving in savings is not a
    homewares purchase). Never a person's choice (a blank they made included), a rule's
    category, a guessed income category, a locked row, or one waiting in a suggestion."""
    cash = categories.get(CASH_DEPOSIT_CATEGORY)
    if cash is None:
        return 0

    def unplaced(t: models.Transaction) -> bool:
        if t.category_id is None:
            return not set_by_person(t)
        guessed = t.confidence is not None and t.confidence < RULE_CONFIDENCE
        return guessed and t.category_id not in income_ids and t.category_id != cash

    filed = 0
    for t in rows:
        account = accounts.get(t.account_id)
        if (
            t.amount_cents > 0
            and account is not None
            and account.type in DEPOSIT_ACCOUNT_TYPES
            and t.transfer_group_id is None
            and t.split_parent_id is None
            and not t.is_transfer
            and not t.category_locked
            and unplaced(t)
            and t.id not in waiting
        ):
            t.category_id = cash
            t.confidence = ML_MAX_CONFIDENCE
            filed += 1
    return filed


def classify_transfers(
    db: Session,
    household_id: str,
    *,
    since: dt.date | None = None,
    window_days: int = WINDOW_DAYS,
) -> TransferScan:
    """Link every pair with evidence, by kind; leave the rest for review; and file
    deposits into savings that came from none of the household's accounts as cash
    deposits. Commits."""
    accounts = _accounts(db, household_id)
    categories = _category_ids(db, household_id)
    cash = categories.get(CASH_DEPOSIT_CATEGORY)
    # Cash deposit is income, whatever kind a household's own category of that name had
    # when it was adopted: a person's or a rule's is never offered as half a transfer.
    income_ids = _income_ids(db, household_id) | ({cash} if cash else set())
    scan = TransferScan()
    pairing = _pairing(
        db, household_id, since=since, window_days=window_days, income_ids=income_ids
    )
    for pair in pairing.pairs:
        if pair.sure:
            apply_kind(db, pair.outflow, pair.inflow, pair.kind, accounts, categories)
            scan.linked.append(pair)
        else:
            scan.to_review.append(pair)
    for pair in scan.to_review:
        t = pair.inflow
        # Filed as a cash deposit automatically before its other half was imported:
        # while a person decides, it is neither.
        if (
            cash is not None
            and t.category_id == cash
            and not t.category_locked
            and not set_by_person(t)
            and (t.confidence or 0) < RULE_CONFIDENCE
        ):
            t.category_id = None
            t.confidence = None
    db.flush()
    conditions = [
        models.Transaction.household_id == household_id,
        models.Transaction.amount_cents > 0,
        models.Transaction.transfer_group_id.is_(None),
    ]
    if since is not None:
        # Only rows whose possible other halves the scan saw: an inflow just before
        # `since` may pair with an outflow the scan never loaded, so it is not known to
        # have come from nowhere.
        conditions.append(models.Transaction.txn_date >= since)
    scan.cash_deposits = _file_cash_deposits(
        db,
        household_id,
        db.execute(select(models.Transaction).where(*conditions)).scalars().all(),
        accounts,
        categories,
        pairing.possible,
        income_ids,
    )
    db.commit()
    return scan


def detect_transfers(
    db: Session,
    household_id: str,
    window_days: int = WINDOW_DAYS,
    *,
    since: dt.date | None = None,
) -> int:
    """The legs linked by a scan: classify_transfers, counted the way imports report it."""
    scan = classify_transfers(db, household_id, since=since, window_days=window_days)
    return 2 * len(scan.linked)


def learn_wording(account: models.Account, description: str) -> str | None:
    """Remember how money going to this account is described, from a pair a person
    accepted: next time, the same wording links it at once. Returns the phrase learned,
    or None when there was nothing worth keeping or it was already known."""
    words = wording_of(description).split()[:6]
    while len(" ".join(words)) > MAX_WORDING_LENGTH and len(words) > 2:
        words.pop()
    phrase = " ".join(words)
    if len(phrase) < 6 or len(words) < 2 or len(phrase) > MAX_WORDING_LENGTH:
        return None
    if len(account.transfer_wording or []) >= MAX_WORDING_PHRASES:
        return None  # what an account can hold; a person can still edit the list
    if any(has_phrase(phrase, p) for p in (account.transfer_wording or [])):
        return None
    account.transfer_wording = [*(account.transfer_wording or []), phrase]
    return phrase


def group_legs(db: Session, household_id: str, group_id: str) -> list[models.Transaction]:
    return list(
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household_id,
                models.Transaction.transfer_group_id == group_id,
            )
        )
        .scalars()
        .all()
    )


def outflow_and_inflow(
    legs: list[models.Transaction],
) -> tuple[models.Transaction, models.Transaction] | None:
    """The pair's two legs. A leg split after linking shares its group with its parts;
    the parts are not legs."""
    legs = [t for t in legs if t.split_parent_id is None]
    outs = [t for t in legs if t.amount_cents < 0]
    ins = [t for t in legs if t.amount_cents > 0]
    if len(outs) == 1 and len(ins) == 1:
        return outs[0], ins[0]
    return None


def dismiss(db: Session, household_id: str, outflow_id: str, inflow_id: str) -> None:
    exists = db.execute(
        select(models.TransferDismissal.id).where(
            models.TransferDismissal.outflow_id == outflow_id,
            models.TransferDismissal.inflow_id == inflow_id,
        )
    ).first()
    if not exists:
        db.add(
            models.TransferDismissal(
                household_id=household_id, outflow_id=outflow_id, inflow_id=inflow_id
            )
        )


def unlink(db: Session, household_id: str, group_id: str) -> int:
    """Undo a link: both legs become ordinary transactions again, and the pair is
    remembered as not a transfer so no scan links it again. Returns the legs released."""
    legs = group_legs(db, household_id, group_id)
    pair = outflow_and_inflow(legs)
    if pair is not None:
        dismiss(db, household_id, pair[0].id, pair[1].id)
    for t in legs:
        release(db, t)
    return len(legs)


def release(db: Session, t: models.Transaction) -> None:
    """Make `t` an ordinary transaction again: its other half was deleted, a person
    said it is not a transfer, or a link was undone.

    Transfer detection filed it under a transfer category, which is now wrong, and
    marked that as automatic: so it goes back to Uncategorised, for review and for
    rules, instead of sitting in Internal transfers looking like a choice someone made.
    A category a person set (1.0) or locked stays, and so does the paying side's
    Mortgage: on its own it is still a repayment someone made.
    """
    was = t.transfer_kind
    t.is_transfer = False
    t.transfer_group_id = None
    t.transfer_kind = None
    if t.category_id is None or t.category_locked or t.split_parent_id is not None:
        return  # a split's parts carry the categories a person gave them
    if t.confidence is None:
        # No score: a person's, except Internal transfers, which older releases wrote
        # unscored when they linked a pair.
        if t.category_id == _category_ids(db, t.household_id).get(TRANSFER_CATEGORY):
            t.category_id = None
        return
    if t.confidence >= 1.0:
        return
    keys = [TRANSFER_CATEGORY, CARD_PAYMENT_CATEGORY]
    if was == LOAN_REPAYMENT and t.amount_cents > 0:
        # The loan's side of a repayment was filed with the paying side's category; on
        # its own it is money arriving in a loan, and a Mortgage label on it would
        # count it as income.
        keys += [MORTGAGE_CATEGORY, LOAN_REPAYMENT_CATEGORY]
    automatic_ids = set(
        db.execute(
            select(models.Category.id).where(
                models.Category.household_id == t.household_id,
                models.Category.system_key.in_(keys),
            )
        ).scalars()
    )
    if t.category_id in automatic_ids:
        t.category_id = None
        t.confidence = None


def rekind_into(db: Session, account: models.Account, old_type: str) -> int:
    """An account's type changed: the pairs into it that were counted by its old type
    are counted by its new one -- a "Westpac" loan imported as an everyday account has
    its repayments linked as money between accounts until its type is put right, and a
    car loan typed as a home loan has them filed as Mortgage. A kind a person chose that
    the old type would not have given stays. Returns the pairs whose kind or automatic
    category changed."""
    was, now = kind_for_type(old_type), kind_for(account)
    kinds = {was}
    if now == LOAN_REPAYMENT:
        kinds.add(LOAN_REPAYMENT)  # home loan <-> personal loan moves the category
    if was == now and old_type == account.type:
        return 0
    inflows = db.execute(
        select(models.Transaction).where(
            models.Transaction.household_id == account.household_id,
            models.Transaction.account_id == account.id,
            models.Transaction.amount_cents > 0,
            models.Transaction.split_parent_id.is_(None),
            models.Transaction.transfer_group_id.is_not(None),
            models.Transaction.transfer_kind.in_(kinds),
        )
    ).scalars().all()
    accounts = _accounts(db, account.household_id)
    categories = _category_ids(db, account.household_id)
    changed = 0
    for inflow in inflows:
        legs = group_legs(db, account.household_id, inflow.transfer_group_id or "")
        pair = outflow_and_inflow(legs)
        if pair is None or pair[1].id != inflow.id:
            continue
        kind = now if inflow.transfer_kind == was else LOAN_REPAYMENT
        before = (pair[0].transfer_kind, pair[0].category_id, pair[1].category_id)
        apply_kind(db, pair[0], pair[1], kind, accounts, categories)
        if (pair[0].transfer_kind, pair[0].category_id, pair[1].category_id) != before:
            changed += 1
    return changed


def unfile_cash_deposits(db: Session, account: models.Account) -> int:
    """The account is no longer savings or offset: money into it is not a cash deposit,
    so the ones Saiva filed there automatically go back to blank. A person's or a
    rule's, or a locked one, stays."""
    if account.type in DEPOSIT_ACCOUNT_TYPES:
        return 0
    cash = _category_ids(db, account.household_id).get(CASH_DEPOSIT_CATEGORY)
    if cash is None:
        return 0
    cleared = 0
    for t in db.execute(
        select(models.Transaction).where(
            models.Transaction.account_id == account.id,
            models.Transaction.category_id == cash,
            models.Transaction.category_locked.is_(False),
        )
    ).scalars():
        if not set_by_person(t) and (t.confidence or 0) < RULE_CONFIDENCE:
            t.category_id = None
            t.confidence = None
            cleared += 1
    return cleared


def classify_existing(db: Session, household_id: str) -> int:
    """Give every pair linked before kinds existed its kind, and count it that way.

    Older releases linked a pair and filed both legs under Internal transfers, whatever
    the accounts: a card payment and a loan repayment alike. Whole groups are read --
    a leg this release kinded and one an older image linked to it after a rollback are
    still one pair. A leg truly on its own -- its partner deleted before deleting
    released it -- becomes ordinary again, and a kind left on a row no longer linked
    (an older image unlinks without knowing the column) is cleared. Returns the pairs
    classified.
    """
    accounts = _accounts(db, household_id)
    categories = _category_ids(db, household_id)
    group_ids = set(
        db.execute(
            select(models.Transaction.transfer_group_id).where(
                models.Transaction.household_id == household_id,
                models.Transaction.transfer_group_id.is_not(None),
                models.Transaction.transfer_kind.is_(None),
            )
        ).scalars()
    )
    legs_by_group: dict[str, list[models.Transaction]] = defaultdict(list)
    if group_ids:
        for t in db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household_id,
                models.Transaction.transfer_group_id.in_(group_ids),
            )
        ).scalars():
            legs_by_group[t.transfer_group_id or ""].append(t)
    classified = 0
    for legs in legs_by_group.values():
        pair = outflow_and_inflow(legs)
        if pair is None:
            if len([t for t in legs if t.split_parent_id is None]) == 1:
                for t in legs:
                    release(db, t)
            continue
        destination = accounts.get(pair[1].account_id)
        kind = kind_for(destination) if destination is not None else OWN_ACCOUNTS
        apply_kind(db, pair[0], pair[1], kind, accounts, categories)
        classified += 1
    for t in db.execute(
        select(models.Transaction).where(
            models.Transaction.household_id == household_id,
            models.Transaction.transfer_group_id.is_(None),
            models.Transaction.transfer_kind.is_not(None),
        )
    ).scalars():
        t.transfer_kind = None
    db.flush()
    return classified
