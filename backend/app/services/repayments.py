"""Home loan repayments: the regular one, and extra ones (0.18.1).

The household says what its regular repayment is -- the amount and how often, from when
(a rate rise is a new amount from a date) -- on the loan's account. A repayment near the
amount in force on its date, once a period, is the regular one, filed as Mortgage; any
other is extra, filed as Extra repayments. Both count as spending. A late or early
repayment, a moved payment day or a missed one is still regular; a second payment within
days of the regular one is not. Two accounts paying parts of it on one day are one
repayment. Until a repayment is set, every repayment is regular, as before -- and Saiva
suggests one from the history, for the household to confirm.

A person's own choice for a repayment, or a locked category, always stays.
"""

from __future__ import annotations

import datetime as dt
import itertools
import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..constants import (
    EXTRA_REPAYMENT_CATEGORY,
    LOAN_REPAYMENT,
    LOAN_REPAYMENT_CATEGORY,
    MORTGAGE_CATEGORY,
)
from .categorise import ML_MAX_CONFIDENCE, set_by_person

EVERY_DAYS = {"weekly": 7.0, "fortnightly": 14.0, "monthly": 30.44}
AMOUNT_TOLERANCE = 0.10  # within 10% of the amount in force: the regular repayment
# Regular repayments are at least this many days apart. Easter holds Friday's repayment
# over to Tuesday, three days before the next one; a second payment two days after the
# regular one is extra.
MIN_GAP_DAYS = {"weekly": 3, "fortnightly": 7, "monthly": 14}
MAX_LEGS_TOGETHER = 6  # legs on one day tried together, as parts of one repayment
SUGGEST_FROM_DAYS = 120  # a suggestion reads the last few months
SUGGEST_AT_LEAST = 3  # repayments at one amount before it is suggested


@dataclass(frozen=True)
class Period:
    since: dt.date | None
    cents: int
    every: str

    def as_json(self) -> dict:
        return {
            "since": self.since.isoformat() if self.since else None,
            "cents": self.cents,
            "every": self.every,
        }


def schedule_of(account: models.Account) -> list[Period]:
    """The account's regular repayment, oldest period first. Empty when not set."""
    periods: list[Period] = []
    for p in account.repayment_schedule or []:
        try:
            since = dt.date.fromisoformat(p["since"]) if p.get("since") else None
            cents, every = int(p["cents"]), str(p["every"])
        except (KeyError, TypeError, ValueError):
            continue
        if cents > 0 and every in EVERY_DAYS:
            periods.append(Period(since, cents, every))
    return sorted(periods, key=lambda p: p.since or dt.date.min)


def in_force(schedule: list[Period], day: dt.date) -> Period:
    """The period a date falls in: the latest begun by then, or the first."""
    current = schedule[0]
    for p in schedule:
        if p.since is None or p.since <= day:
            current = p
    return current


def repayment_category_ids(db: Session, household_id: str) -> tuple[str | None, str | None]:
    """(Mortgage, Extra repayments) for the household, by system key."""
    found = dict(
        db.execute(
            select(models.Category.system_key, models.Category.id).where(
                models.Category.household_id == household_id,
                models.Category.system_key.in_([MORTGAGE_CATEGORY, EXTRA_REPAYMENT_CATEGORY]),
            )
        ).tuples().all()
    )
    return found.get(MORTGAGE_CATEGORY), found.get(EXTRA_REPAYMENT_CATEGORY)


def _loans(db: Session, household_id: str, types: Iterable[str]) -> list[models.Account]:
    return list(
        db.execute(
            select(models.Account).where(
                models.Account.household_id == household_id,
                models.Account.type.in_(list(types)),
            )
        ).scalars()
    )


def paying_legs(
    db: Session, household_id: str, loan_ids: list[str]
) -> dict[str, list[models.Transaction]]:
    """Each loan's repayments, as the outflows that paid them, oldest first."""
    if not loan_ids:
        return {}
    arrivals = db.execute(
        select(models.Transaction.transfer_group_id, models.Transaction.account_id).where(
            models.Transaction.household_id == household_id,
            models.Transaction.account_id.in_(loan_ids),
            models.Transaction.transfer_kind == LOAN_REPAYMENT,
            models.Transaction.amount_cents > 0,
            models.Transaction.split_parent_id.is_(None),
        )
    ).tuples().all()
    loan_of = {group: loan for group, loan in arrivals if group}
    by_loan: dict[str, list[models.Transaction]] = {loan: [] for loan in loan_ids}
    if not loan_of:
        return by_loan
    for t in db.execute(
        select(models.Transaction).where(
            models.Transaction.household_id == household_id,
            models.Transaction.transfer_group_id.in_(list(loan_of)),
            models.Transaction.amount_cents < 0,
            models.Transaction.split_parent_id.is_(None),
        )
    ).scalars():
        by_loan[loan_of[t.transfer_group_id or ""]].append(t)
    for legs in by_loan.values():
        legs.sort(key=lambda t: (t.txn_date, t.id))
    return by_loan


def _near(cents: int, target: int) -> bool:
    return abs(cents - target) <= AMOUNT_TOLERANCE * target


def _best_parts(
    free: list[models.Transaction], base: int, target: int
) -> list[models.Transaction] | None:
    """The day's legs that, with what is already regular that day (`base`), make the
    repayment: the closest to the amount, then the fewest legs. None when nothing does."""
    best: tuple[int, int, list[str]] | None = None
    chosen: list[models.Transaction] | None = None
    pool = sorted(free, key=lambda t: t.id)[:MAX_LEGS_TOGETHER]
    for size in range(0 if base else 1, len(pool) + 1):
        for parts in itertools.combinations(pool, size):
            total = base + sum(abs(t.amount_cents) for t in parts)
            if not _near(total, target):
                continue
            key = (abs(total - target), size, [t.id for t in parts])
            if best is None or key < best:
                best, chosen = key, list(parts)
    return chosen


def sort_legs(
    legs: list[models.Transaction], schedule: list[Period], fixed: dict[str, bool]
) -> dict[str, bool]:
    """Which of one loan's repayments are extra, by id (True: extra). `fixed` holds what
    a person (or a lock) has said. With no schedule, none is extra but what was said."""
    extra = {t.id: fixed.get(t.id, False) for t in legs}
    if not schedule:
        return extra
    by_day: dict[dt.date, list[models.Transaction]] = {}
    for t in legs:
        by_day.setdefault(t.txn_date, []).append(t)
    last_regular: dt.date | None = None
    for day in sorted(by_day):
        period = in_force(schedule, day)
        spacing = MIN_GAP_DAYS[period.every]
        day_legs = by_day[day]
        said_regular = [t for t in day_legs if fixed.get(t.id) is False]
        free = [t for t in day_legs if t.id not in fixed]
        spaced = last_regular is None or (day - last_regular).days >= spacing
        regular: list[models.Transaction] = []
        if free and (spaced or said_regular):
            base = sum(abs(t.amount_cents) for t in said_regular)
            regular = _best_parts(free, base, period.cents) or []
        for t in free:
            extra[t.id] = t not in regular
        if said_regular or regular:
            last_regular = day
    return extra


def suggest(legs: list[models.Transaction], today: dt.date) -> Period | None:
    """A regular repayment the recent history shows: the amount most of the recent
    repayments are near, how often they go, and since when. For a person to confirm."""
    recent = [t for t in legs if t.txn_date >= today - dt.timedelta(days=SUGGEST_FROM_DAYS)]
    if len(recent) < SUGGEST_AT_LEAST:
        return None
    days: dict[dt.date, int] = {}
    for t in recent:
        days[t.txn_date] = days.get(t.txn_date, 0) + abs(t.amount_cents)
    amounts = [abs(t.amount_cents) for t in recent] + list(days.values())
    support = {a: sum(1 for b in amounts if _near(b, a)) for a in amounts}
    usual = max(support, key=lambda a: (support[a], a))
    on = sorted(
        {t.txn_date for t in recent if _near(abs(t.amount_cents), usual)}
        | {d for d, total in days.items() if _near(total, usual)}
    )
    if len(on) < SUGGEST_AT_LEAST:
        return None
    gaps = [(b - a).days for a, b in zip(on, on[1:], strict=False)]
    gap = statistics.median(gaps)
    every = "weekly" if gap <= 10 else "fortnightly" if gap <= 21 else "monthly"
    cents = int(round(statistics.median(
        [a for a in amounts if _near(a, usual)]
    )))
    # Since when: back through the history while the repayments stay near this amount.
    since = on[0]
    for t in sorted(legs, key=lambda t: t.txn_date, reverse=True):
        if t.txn_date >= since:
            continue
        if _near(abs(t.amount_cents), cents):
            since = t.txn_date
        elif (on[0] - t.txn_date).days > EVERY_DAYS[every] * 2:
            break
    return Period(since, cents, every)


def suggestion_for(
    db: Session, account: models.Account, today: dt.date | None = None
) -> Period | None:
    """What to offer on a home loan's account: a regular repayment when none is set, or a
    new one when the recent repayments have moved away from the one set."""
    if account.type != "home_loan":
        return None
    today = today or dt.date.today()
    legs = paying_legs(db, account.household_id, [account.id]).get(account.id, [])
    found = suggest(legs, today)
    if found is None:
        return None
    schedule = schedule_of(account)
    if not schedule:
        return found
    now = in_force(schedule, today)
    if _near(found.cents, now.cents) and found.every == now.every:
        return None
    if now.since and found.since and found.since <= now.since:
        found = Period(now.since, found.cents, found.every)
    return found


def _fixed(
    legs: list[models.Transaction], mortgage: str | None, extra_id: str | None
) -> dict[str, bool]:
    """What a person (or a lock) has already said: Mortgage is regular, Extra is extra."""
    return {
        t.id: t.category_id == extra_id
        for t in legs
        if (t.category_locked or set_by_person(t)) and t.category_id in (mortgage, extra_id)
    }


def sort_repayments(db: Session, household_id: str) -> int:
    """File each home loan repayment as Mortgage or Extra repayments, by the loan's
    regular repayment -- and its loan side to match. Only what Saiva filed is moved: a
    person's or a locked category stays. Returns the repayments re-filed. Does not
    commit."""
    mortgage, extra_id = repayment_category_ids(db, household_id)
    if mortgage is None or extra_id is None:
        return 0
    loans = {a.id: a for a in _loans(db, household_id, ["home_loan"])}
    changed = 0
    for loan_id, legs in paying_legs(db, household_id, list(loans)).items():
        fixed = _fixed(legs, mortgage, extra_id)
        flags = sort_legs(legs, schedule_of(loans[loan_id]), fixed)
        arrivals = {
            t.transfer_group_id: t
            for t in db.execute(
                select(models.Transaction).where(
                    models.Transaction.household_id == household_id,
                    models.Transaction.account_id == loan_id,
                    models.Transaction.transfer_group_id.in_(
                        [t.transfer_group_id for t in legs if t.transfer_group_id]
                    ),
                    models.Transaction.amount_cents > 0,
                )
            ).scalars()
        }
        for t in legs:
            target = extra_id if flags[t.id] else mortgage
            sides = [t, arrivals.get(t.transfer_group_id)]
            for leg in sides:
                if leg is None or leg.category_locked or set_by_person(leg):
                    continue
                if leg.category_id not in (mortgage, extra_id) or leg.category_id == target:
                    continue
                leg.category_id = target
                leg.confidence = ML_MAX_CONFIDENCE
                changed += int(leg is t)
    return changed


def _belongs(
    t: models.Transaction,
    loan: models.Account,
    schedule: list[Period],
    payers: set[str],
    merchants: set[str],
) -> bool:
    """Whether a debit not linked to anything is this loan's repayment: it carries the
    loan's own wording, or it is from an account that paid the loan, by the same
    description, near the amount."""
    from .transfers import has_phrase, wording_of  # noqa: PLC0415 - transfers imports this

    words = wording_of(t.raw_description or "")
    if any(has_phrase(words, p) for p in loan.transfer_wording or []):
        return True
    return (
        t.account_id in payers
        and t.merchant in merchants
        and _near(abs(t.amount_cents), in_force(schedule, t.txn_date).cents)
    )


@dataclass
class LoanBill:
    """A loan's regular repayment, as the bills show it."""

    loan: models.Account
    category_id: str | None
    every: str
    typical_cents: int
    first_date: dt.date
    last_date: dt.date
    occurrences: int


def loan_bills(
    db: Session, household_id: str, start: dt.date, today: dt.date
) -> tuple[list[LoanBill], set[str]]:
    """Each loan with a regular repayment set, as a bill, and the transactions that make
    it up -- left out of the bills found by description, so none is counted twice. That
    includes its debits not linked yet (the loan's statement not imported that far): by
    the loan's own wording, or from an account that paid it, by description, near the
    amount. A loan without a repayment set is found by description, as before."""
    mortgage, _ = repayment_category_ids(db, household_id)
    loan_repayments = db.execute(
        select(models.Category.id).where(
            models.Category.household_id == household_id,
            models.Category.system_key == LOAN_REPAYMENT_CATEGORY,
        )
    ).scalar()
    scheduled = {
        a.id: a
        for a in db.execute(
            select(models.Account).where(
                models.Account.household_id == household_id,
                models.Account.repayment_schedule.is_not(None),
            )
        ).scalars()
        if schedule_of(a)
    }
    if not scheduled:
        return [], set()
    _, extra_id = repayment_category_ids(db, household_id)
    legs_by_loan = paying_legs(db, household_id, list(scheduled))
    bills: list[LoanBill] = []
    covered: set[str] = set()
    loose = db.execute(
        select(models.Transaction).where(
            models.Transaction.household_id == household_id,
            models.Transaction.transfer_group_id.is_(None),
            models.Transaction.is_transfer.is_(False),
            models.Transaction.split_parent_id.is_(None),
            models.Transaction.amount_cents < 0,
            models.Transaction.txn_date >= start,
            models.Transaction.txn_date <= today,
        )
    ).scalars().all()
    for loan_id, legs in legs_by_loan.items():
        loan = scheduled[loan_id]
        schedule = schedule_of(loan)
        payers = {t.account_id for t in legs}
        merchants = {t.merchant for t in legs if t.merchant}
        mine = [t for t in loose if _belongs(t, loan, schedule, payers, merchants)]
        everything = sorted(
            [t for t in legs if start <= t.txn_date <= today] + mine,
            key=lambda t: (t.txn_date, t.id),
        )
        covered.update(t.id for t in legs)
        covered.update(t.id for t in mine)
        flags = sort_legs(everything, schedule, _fixed(everything, mortgage, extra_id))
        regular_days = sorted({t.txn_date for t in everything if not flags[t.id]})
        if not regular_days:
            continue
        now = in_force(schedule, today)
        bills.append(
            LoanBill(
                loan=loan,
                category_id=mortgage if loan.type == "home_loan" else loan_repayments,
                every=now.every,
                typical_cents=now.cents,
                first_date=regular_days[0],
                last_date=regular_days[-1],
                occurrences=len(regular_days),
            )
        )
    return bills, covered
