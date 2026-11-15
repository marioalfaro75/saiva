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


def looks_like_a_transfer(out: models.Transaction, inflow: models.Transaction) -> bool:
    """Whether the bank's own wording suggests these two are the same money.

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


def kind_for(destination: models.Account) -> str:
    """What a pair is, by the account the money went into."""
    if destination.type == "credit_card":
        return CARD_PAYMENT
    if destination.type in LOAN_ACCOUNT_TYPES:
        return LOAN_REPAYMENT
    return OWN_ACCOUNTS


def _account_digits(account: models.Account) -> str | None:
    digits = re.sub(r"\D", "", account.bank_identifier or "")
    return digits[-4:] if len(digits) >= 4 else None


def evidence_for(
    out: models.Transaction,
    inflow: models.Transaction,
    accounts: dict[str, models.Account],
) -> str | None:
    """Why these two are one amount of money moving, in a few words -- or None when
    nothing beyond the amount and the dates says so."""
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
            if wording_of(phrase) and wording_of(phrase) in words:
                return f"“{phrase}” is your wording for {account.name}"
    for account, _, text in sides:
        digits = _account_digits(account)
        if digits and any(run.endswith(digits) for run in re.findall(r"\d{4,}", text or "")):
            return f"mentions {account.name}'s account number"
    if looks_like_a_transfer(out, inflow):
        return "your bank's transfer wording"
    if destination is not None:
        arriving = ARRIVING_WORDS.get(destination.type, ())
        if any(word in _text(inflow) for word in arriving):
            return f"a payment arriving in {destination.name}"
    return None


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


def find_pairs(
    db: Session,
    household_id: str,
    *,
    since: dt.date | None = None,
    window_days: int = WINDOW_DAYS,
) -> list[Pair]:
    """Every pair not yet linked: the ones with evidence, then the rest for review.

    Deterministic: outflows in date order, each taking the nearest inflow -- with
    evidence first, then by the fewest days between, then the earlier. A pair a person
    dismissed is never offered again.
    """
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
    rows = db.execute(select(models.Transaction).where(*conditions)).scalars().all()
    accounts = _accounts(db, household_id)
    dismissed = _dismissed(db, household_id)
    income_ids = _income_ids(db, household_id)

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
    used: set[str] = set()
    pairs: list[Pair] = []

    def gap(out: models.Transaction, i: models.Transaction) -> tuple[int, dt.date, str]:
        return (abs((i.txn_date - out.txn_date).days), i.txn_date, i.id)

    # Pass 1: pairs with evidence, so a pair the bank's wording ties together is never
    # lost to a nearer inflow that only shares the amount.
    for out in outflows:
        sure = [
            (gap(out, i), i, why)
            for i in considered[out.id]
            if i.id not in used and (why := evidence_for(out, i, accounts)) is not None
        ]
        if sure:
            _, inflow, why = min(sure, key=lambda s: s[0])
            pairs.append(Pair(out, inflow, kind_for(accounts[inflow.account_id]), why))
            used.update({out.id, inflow.id})
    # Pass 2: what is left that agrees on amount and dates only, for a person to decide.
    for out in outflows:
        if out.id in used:
            continue
        rest = [
            i
            for i in considered[out.id]
            if i.id not in used and not _income_already(i, income_ids)
        ]
        if rest:
            inflow = min(rest, key=lambda i: gap(out, i))
            pairs.append(Pair(out, inflow, kind_for(accounts[inflow.account_id]), None))
            used.update({out.id, inflow.id})
    return pairs


def _may_refile(t: models.Transaction, automatic_ids: Iterable[str]) -> bool:
    """Whether linking may set this leg's category: not a locked one, nor one a person
    chose -- except a category transfer detection itself put there, which older
    releases left unscored, so it read as a person's."""
    if t.category_locked:
        return False
    return not set_by_person(t) or t.category_id in set(automatic_ids)


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
    automatic = [
        categories[k]
        for k in (TRANSFER_CATEGORY, CARD_PAYMENT_CATEGORY, CASH_DEPOSIT_CATEGORY)
        if k in categories
    ]
    for t in (outflow, inflow):
        t.transfer_group_id = group
        t.transfer_kind = kind
        t.is_transfer = not (kind == LOAN_REPAYMENT and t is outflow)
        if category is not None and _may_refile(t, automatic) and t.category_id != category:
            t.category_id = category
            # Automatic, not a person's: the score a row carried was for the category
            # this replaces, and a person's 1.0 for Coffee left on it would read as a
            # person's choice of Credit-card payments.
            t.confidence = ML_MAX_CONFIDENCE


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
    scan = TransferScan()
    for pair in find_pairs(db, household_id, since=since, window_days=window_days):
        if pair.sure:
            apply_kind(db, pair.outflow, pair.inflow, pair.kind, accounts, categories)
            scan.linked.append(pair)
        else:
            scan.to_review.append(pair)
    db.flush()
    conditions = [
        models.Transaction.household_id == household_id,
        models.Transaction.amount_cents > 0,
        models.Transaction.transfer_group_id.is_(None),
    ]
    if since is not None:
        conditions.append(models.Transaction.txn_date >= since - dt.timedelta(days=window_days))
    waiting = {p.inflow.id for p in scan.to_review}
    scan.cash_deposits = _file_cash_deposits(
        db,
        household_id,
        db.execute(select(models.Transaction).where(*conditions)).scalars().all(),
        accounts,
        categories,
        waiting,
        _income_ids(db, household_id),
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
    phrase = " ".join(wording_of(description).split()[:6])
    if len(phrase) < 6 or len(phrase.split()) < 2:
        return None
    known = [wording_of(p) for p in (account.transfer_wording or [])]
    if any(k and k in phrase for k in known):
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
    if t.category_id is None or t.category_locked or (t.confidence or 0) >= 1.0:
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


def classify_existing(db: Session, household_id: str) -> int:
    """Give every pair linked before kinds existed its kind, and count it that way.

    Older releases linked a pair and filed both legs under Internal transfers, whatever
    the accounts: a card payment and a loan repayment alike. A leg on its own -- its
    partner deleted before deleting released it -- becomes ordinary again. Returns the
    pairs classified.
    """
    accounts = _accounts(db, household_id)
    categories = _category_ids(db, household_id)
    legs_by_group: dict[str, list[models.Transaction]] = defaultdict(list)
    for t in db.execute(
        select(models.Transaction).where(
            models.Transaction.household_id == household_id,
            models.Transaction.transfer_group_id.is_not(None),
            models.Transaction.transfer_kind.is_(None),
        )
    ).scalars():
        legs_by_group[t.transfer_group_id or ""].append(t)
    classified = 0
    for legs in legs_by_group.values():
        pair = outflow_and_inflow(legs)
        if pair is None:
            if len(legs) == 1:
                release(db, legs[0])
            continue
        destination = accounts.get(pair[1].account_id)
        kind = kind_for(destination) if destination is not None else OWN_ACCOUNTS
        apply_kind(db, pair[0], pair[1], kind, accounts, categories)
        classified += 1
    db.flush()
    return classified
