"""Home loan repayments: the regular one, and extra ones (PRD: transfers, 0.18.1).

Both count as spending, once, on the account that paid. What tells them apart is the
household's own pattern: the usual amount and how often it goes. A repayment near the
usual amount, a period after the last regular one, is regular. One far from it, or a
second within the same period, is extra -- unless the new amount comes again a period
later, which is the usual changing (a rate rise, say): two in a row and it is the new
usual. A person's own choice for a repayment stays, and teaches the pattern.

Kept in the category: Mortgage for a regular repayment, Extra repayments for an extra.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models
from ..constants import (
    EXTRA_REPAYMENT_CATEGORY,
    LOAN_ACCOUNT_TYPES,
    LOAN_REPAYMENT,
    LOAN_REPAYMENT_CATEGORY,
    MORTGAGE_CATEGORY,
)
from .categorise import ML_MAX_CONFIDENCE, RULE_CONFIDENCE, set_by_person

USUAL_FROM = 8  # the usual amount, from the last few regular repayments
AMOUNT_TOLERANCE = 0.10  # within 10% of each other: the same repayment
# How far from its schedule a regular repayment may land: a day or two early, or a few
# days late -- a public holiday pushes Friday's repayment to Tuesday.
LATE_DAYS = 4
EARLY_DAYS = 3
# A gap this long ends a run: after a repayment holiday the schedule starts again.
RUN_BREAK_PERIODS = 3
SAME_WEEK_DAYS = 5  # before any schedule is known, sooner than this is the same week
ESTABLISHED = 3  # repayments in a run before it outweighs a larger, shorter one

# The schedules a repayment keeps, and how far a gap may stray from a whole number of
# them: weekly, fortnightly, monthly.
_CADENCES = ((7.0, 2), (14.0, 3), (30.44, 5))


@dataclass
class Unit:
    """One payment: a repayment's legs on one day, together -- two partners each
    paying part of it from their own account are one repayment."""

    legs: list[models.Transaction]
    date: dt.date
    amount: int


@dataclass
class Chain:
    """A run of regular repayments: a similar amount on a steady schedule."""

    units: list[Unit]
    cadence: float

    @property
    def start(self) -> dt.date:
        return self.units[0].date

    @property
    def end(self) -> dt.date:
        return self.units[-1].date

    def usual(self) -> int:
        return int(round(statistics.median(u.amount for u in self.units[-USUAL_FROM:])))


@dataclass
class Analysis:
    extra: dict[str, bool]  # by leg id
    chains: list[Chain]  # the regular runs, oldest first


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


def _cadence(gaps: list[int]) -> float | None:
    """The schedule that explains the gaps best: a gap of two weeks is a weekly payer's
    missed week as readily as a fortnight, so each schedule is scored by the gaps it
    explains, less the payments it would have to have missed."""
    best: tuple[float, float] | None = None
    for cadence, tolerance in _CADENCES:
        explained = missed = 0
        for gap in gaps:
            if gap < SAME_WEEK_DAYS or gap > RUN_BREAK_PERIODS * cadence:
                continue
            k = round(gap / cadence)
            if k >= 1 and abs(gap - k * cadence) <= tolerance:
                explained += 1
                missed += k - 1
        score = 2 * explained - missed
        if explained and score > 0 and (best is None or score > best[0]):
            best = (score, cadence)
    return best[1] if best else None


def _clusters(units: list[Unit]) -> list[list[Unit]]:
    """Units of a similar amount together, each cluster in date order."""
    by_amount = sorted(units, key=lambda u: (u.amount, u.date, u.legs[0].id))
    clusters: list[list[Unit]] = []
    for u in by_amount:
        if clusters and u.amount <= clusters[-1][-1].amount * (1 + AMOUNT_TOLERANCE):
            clusters[-1].append(u)
        else:
            clusters.append([u])
    return [sorted(c, key=lambda u: (u.date, u.legs[0].id)) for c in clusters]


def _on_schedule(run: list[Unit], cadence: float) -> tuple[list[Unit], list[Unit]]:
    """Which of a run's units fill its schedule's slots, one each: the rest are a second
    payment in a slot already paid. Slots run from the first unit, every `cadence` days;
    each slot takes the unit that lands closest to it, lateness forgiven before
    earliness."""
    anchor = run[0].date
    last = (run[-1].date - anchor).days
    slots = int(last / cadence) + 2
    options = []
    for i, u in enumerate(run):
        offset = (u.date - anchor).days
        for k in range(max(0, int((offset - LATE_DAYS) / cadence)), slots):
            deviation = offset - k * cadence
            if -EARLY_DAYS <= deviation <= LATE_DAYS:
                cost = deviation if deviation >= 0 else 1.5 * -deviation
                options.append((cost, u.date, i, k))
    used_units: set[int] = set()
    used_slots: set[int] = set()
    for _, _, i, k in sorted(options):
        if i in used_units or k in used_slots:
            continue
        used_units.add(i)
        used_slots.add(k)
    kept = [u for i, u in enumerate(run) if i in used_units]
    return kept, [u for i, u in enumerate(run) if i not in used_units]


def _runs(cluster: list[Unit], cadence: float) -> list[list[Unit]]:
    """A cluster split where the gap is long enough to start the schedule again."""
    runs: list[list[Unit]] = [[cluster[0]]]
    for u in cluster[1:]:
        if (u.date - runs[-1][-1].date).days > RUN_BREAK_PERIODS * cadence:
            runs.append([u])
        else:
            runs[-1].append(u)
    return runs


def _classify(units: list[Unit], fixed: dict[str, bool]) -> tuple[set[int], list[Chain]]:
    """Which units (by position) are regular, and the runs they make."""
    def fixed_as(u: Unit) -> bool | None:
        said = [fixed[t.id] for t in u.legs if t.id in fixed]
        if not said:
            return None
        return any(said)  # True: someone said extra

    index = {id(u): i for i, u in enumerate(units)}
    candidates = [u for u in units if fixed_as(u) is not True]
    chains: list[tuple[tuple[int, int, int, int], Chain]] = []
    for cluster in _clusters(candidates):
        gaps = [(b.date - a.date).days for a, b in zip(cluster, cluster[1:], strict=False)]
        cadence = _cadence(gaps)
        if cadence is None:
            continue
        for run in _runs(cluster, cadence):
            kept, _ = _on_schedule(run, cadence) if len(run) > 1 else (run, [])
            if len(kept) < 2:
                continue
            chain = Chain(kept, cadence)
            said_regular = any(fixed_as(u) is False for u in kept)
            established = len(kept) >= ESTABLISHED
            chains.append(
                ((int(said_regular), int(established), chain.usual(), len(kept)), chain)
            )

    # Two runs at the same time: the one a person called regular is the repayment; then
    # an established run (a standing extra is small; a pair of lumps is not established);
    # then the larger. The other is extra -- all of it, unless most of it lies clear of
    # the repayment (the usual changing as one run ends and the next begins).
    accepted: list[Chain] = []
    regular: set[int] = set()
    for _, chain in sorted(chains, key=lambda c: c[0], reverse=True):
        # Inside another run's span, not merely next to it: a run that ends as the
        # next begins is the usual changing, not a second payment alongside.
        clear = [
            u for u in chain.units if not any(o.start <= u.date <= o.end for o in accepted)
        ]
        if len(clear) == len(chain.units) or (
            len(clear) >= 2 and len(clear) * 2 >= len(chain.units)
        ):
            kept_chain = Chain(clear, chain.cadence)
            accepted.append(kept_chain)
            regular.update(index[id(u)] for u in clear)

    if accepted:
        # Before any run started, a payment on the first run's schedule is an earlier
        # regular one (the usual before a change, say); anything else is extra.
        first = min(accepted, key=lambda c: c.start)
        for u in units:
            if u.date >= first.start or index[id(u)] in regular or fixed_as(u) is not None:
                continue
            behind = (first.start - u.date).days
            nearest = round(behind / first.cadence) * first.cadence
            if nearest and -EARLY_DAYS <= nearest - behind <= LATE_DAYS:
                regular.add(index[id(u)])
    else:
        # No schedule to go by yet: the first is regular, a second within the same week
        # is extra, and the rest are regular until a pattern shows.
        last: dt.date | None = None
        for i, u in enumerate(units):
            if fixed_as(u) is True:
                continue
            if last is None or (u.date - last).days >= SAME_WEEK_DAYS:
                regular.add(i)
                last = u.date
    for i, u in enumerate(units):
        said = fixed_as(u)
        if said is False:
            regular.add(i)
        elif said is True:
            regular.discard(i)
    accepted.sort(key=lambda c: c.start)
    return regular, accepted


def _units(
    legs: list[models.Transaction], fixed: dict[str, bool], split: set[dt.date]
) -> list[Unit]:
    by_day: dict[dt.date, list[models.Transaction]] = {}
    for t in legs:
        by_day.setdefault(t.txn_date, []).append(t)
    units: list[Unit] = []
    for day in sorted(by_day):
        day_legs = sorted(by_day[day], key=lambda t: t.id)
        if day in split or any(t.id in fixed for t in day_legs):
            units.extend(Unit([t], day, abs(t.amount_cents)) for t in day_legs)
        else:
            units.append(Unit(day_legs, day, sum(abs(t.amount_cents) for t in day_legs)))
    return units


def analyse(legs: list[models.Transaction], fixed: dict[str, bool] | None = None) -> Analysis:
    """Which of one loan's repayments are extra, and its regular runs. `fixed` holds the
    ones a person decided (True: extra), taken as said and given weight."""
    fixed = fixed or {}
    units = _units(legs, fixed, set())
    regular, chains = _classify(units, fixed)
    # A day whose legs together are not a regular repayment may still hold one: look at
    # its legs one by one (the repayment, and an extra paid the same day).
    split = {u.date for i, u in enumerate(units) if i not in regular and len(u.legs) > 1}
    if split:
        units = _units(legs, fixed, split)
        regular, chains = _classify(units, fixed)
    extra = {t.id: i not in regular for i, u in enumerate(units) for t in u.legs}
    return Analysis(extra, chains)


def sort_out(
    legs: list[models.Transaction], fixed: dict[str, bool] | None = None
) -> dict[str, bool]:
    """Which of one loan's repayments are extra, by id."""
    return analyse(legs, fixed).extra


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
    """File each home loan repayment as Mortgage or Extra repayments, by the pattern.
    Only what Saiva filed is moved; a person's, a rule's or a locked category stays (and
    a person's teaches the pattern). Returns the repayments re-filed. Does not commit."""
    mortgage, extra_id = repayment_category_ids(db, household_id)
    if mortgage is None or extra_id is None:
        return 0
    loans = _loans(db, household_id, ["home_loan"])
    changed = 0
    for legs in paying_legs(db, household_id, [a.id for a in loans]).values():
        fixed = _fixed(legs, mortgage, extra_id)
        by_id = {t.id: t for t in legs}
        for t_id, is_extra in analyse(legs, fixed).extra.items():
            t = by_id[t_id]
            if t_id in fixed or t.category_id not in (mortgage, extra_id):
                continue  # a person's, or filed elsewhere -- a rule's, say
            if (t.confidence or 0) >= RULE_CONFIDENCE:
                continue
            target = extra_id if is_extra else mortgage
            if t.category_id != target:
                t.category_id = target
                t.confidence = ML_MAX_CONFIDENCE
                changed += 1
    return changed


@dataclass
class LoanBill:
    """A loan's regular repayment, as the bills show it."""

    loan: models.Account
    category_id: str | None
    cadence: float
    typical_cents: int
    first_date: dt.date
    last_date: dt.date
    occurrences: int


def loan_bills(
    db: Session, household_id: str, start: dt.date, today: dt.date
) -> tuple[list[LoanBill], set[str]]:
    """Each loan's regular repayment, and the merchants its payments go by. Every account
    repayments are linked into counts, whatever its type; and the same repayment's
    debits not linked yet (the loan's statement not imported that far) count with it,
    rather than as a second bill under the paying account's description."""
    mortgage, extra_id = repayment_category_ids(db, household_id)
    loan_repayments = dict(
        db.execute(
            select(models.Category.system_key, models.Category.id).where(
                models.Category.household_id == household_id,
                models.Category.system_key == LOAN_REPAYMENT_CATEGORY,
            )
        ).tuples().all()
    ).get(LOAN_REPAYMENT_CATEGORY)
    receiving = set(
        db.execute(
            select(models.Transaction.account_id).where(
                models.Transaction.household_id == household_id,
                models.Transaction.transfer_kind == LOAN_REPAYMENT,
                models.Transaction.amount_cents > 0,
            ).distinct()
        ).scalars()
    )
    accounts = {
        a.id: a
        for a in db.execute(
            select(models.Account).where(
                models.Account.household_id == household_id,
                or_(
                    models.Account.type.in_(list(LOAN_ACCOUNT_TYPES)),
                    models.Account.id.in_(receiving),
                ),
            )
        ).scalars()
    }
    legs_by_loan = paying_legs(db, household_id, list(accounts))
    merchants_of: dict[str, str] = {}
    for loan_id, legs in sorted(legs_by_loan.items(), key=lambda kv: -len(kv[1])):
        for t in legs:
            if t.merchant:
                merchants_of.setdefault(t.merchant, loan_id)
    if merchants_of:
        for t in db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household_id,
                models.Transaction.transfer_group_id.is_(None),
                models.Transaction.is_transfer.is_(False),
                models.Transaction.split_parent_id.is_(None),
                models.Transaction.amount_cents < 0,
                models.Transaction.merchant.in_(list(merchants_of)),
            )
        ).scalars():
            legs_by_loan[merchants_of[t.merchant or ""]].append(t)
    bills: list[LoanBill] = []
    for loan_id, legs in legs_by_loan.items():
        loan = accounts[loan_id]
        legs = sorted(
            (t for t in legs if start <= t.txn_date <= today), key=lambda t: (t.txn_date, t.id)
        )
        if not legs:
            continue
        home = loan.type == "home_loan"
        found = analyse(legs, _fixed(legs, mortgage, extra_id) if home else None)
        if not found.chains:
            continue
        latest = max(found.chains, key=lambda c: c.end)
        regular_days = sorted({t.txn_date for t in legs if not found.extra[t.id]})
        bills.append(
            LoanBill(
                loan=loan,
                category_id=mortgage if home else loan_repayments,
                cadence=latest.cadence,
                typical_cents=latest.usual(),
                first_date=regular_days[0],
                last_date=latest.end,
                occurrences=len(regular_days),
            )
        )
    return bills, set(merchants_of)
