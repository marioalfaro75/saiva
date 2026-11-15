"""The Mortgage page (0.19.0): what is owed on a home loan, when it will be paid off, and
what extra repayments, the offset and a change of rate do to that.

Interest is worked out the way Australian lenders do it: each day, on what is owed less
what is in the offset accounts, at the rate that day; and charged once a month. The
regular repayment is the one set on the loan's account (0.18.1), on its due dates. Extras
are on top.

What is owed comes from the loan's account: its balance is what is owed (a loan account
reads negative). The household says what it is on a date when it sets the loan up, and a
loan statement imported after that keeps it right. From the last of those to today it is
an estimate: the repayments Saiva has seen go out since, then the regular repayment.
"""

from __future__ import annotations

import calendar
import datetime as dt
import statistics
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from .balances import balance_through, set_balance
from .repayments import Period, in_force, loan_repayments, schedule_of

__all__ = ["balance_through", "set_balance"]

MAX_YEARS = 50  # not paid off by then: shown as not within 50 years
USUAL_EXTRA_MONTHS = 6  # the usual extras: the typical month of the last six
PAID_MONTHS = 12  # the repayments listed, by month
PER_YEAR = {"weekly": 52, "fortnightly": 26, "monthly": 12}
MAX_RATE = 30.0
USUAL_DAY_FROM = 6  # the usual payment day: the commonest of the last few repayments


@dataclass(frozen=True)
class Rate:
    since: dt.date | None  # None: from the start
    percent: float

    def as_json(self) -> dict:
        return {"since": self.since.isoformat() if self.since else None, "percent": self.percent}


def rates_of(mortgage: models.Mortgage) -> list[Rate]:
    """The loan's interest rate over time, oldest first."""
    rates: list[Rate] = []
    for r in mortgage.rates or []:
        try:
            since = dt.date.fromisoformat(r["since"]) if r.get("since") else None
            percent = float(r["percent"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= percent <= MAX_RATE:
            rates.append(Rate(since, percent))
    return sorted(rates, key=lambda r: r.since or dt.date.min)


def rate_on(rates: list[Rate], day: dt.date) -> float:
    """The rate on a day: the latest begun by then (the first, before any)."""
    current = rates[0]
    for r in rates:
        if r.since is None or r.since <= day:
            current = r
    return current.percent


# ------------------------------------------------------------------ what is owed


def _last_txn_date(db: Session, account: models.Account, today: dt.date) -> dt.date | None:
    return db.execute(
        select(func.max(models.Transaction.txn_date)).where(
            models.Transaction.account_id == account.id,
            models.Transaction.split_parent_id.is_(None),
            models.Transaction.txn_date <= today,
        )
    ).scalar()


def charge_day_of(db: Session, loan: models.Account) -> int | None:
    """The day of the month the lender charges interest, from the loan's statement (31
    for a month's last day): the day of its debits that say "interest", or else the day
    the most is debited on -- interest dwarfs a fee. None without any debits."""
    rows = db.execute(
        select(
            models.Transaction.txn_date,
            models.Transaction.amount_cents,
            models.Transaction.raw_description,
        )
        .where(
            models.Transaction.account_id == loan.id,
            models.Transaction.amount_cents < 0,
            models.Transaction.split_parent_id.is_(None),
        )
        .order_by(models.Transaction.txn_date.desc())
        .limit(24)
    ).tuples().all()
    interest = [r for r in rows if "interest" in (r[2] or "").lower()]
    totals: Counter[int] = Counter()
    for day, cents, _ in interest or rows:
        totals[31 if day.day == calendar.monthrange(day.year, day.month)[1] else day.day] += abs(
            cents
        )
    return max(totals, key=lambda d: totals[d]) if totals else None


def known_owing(db: Session, loan: models.Account, today: dt.date) -> tuple[dt.date, int]:
    """(day, owed): the latest day the books say what is owed -- the day the household
    said (the loan account's balance anchor), or the end of the loan's statement."""
    days = [d for d in (loan.balance_anchor_on, _last_txn_date(db, loan, today)) if d]
    day = max(days) if days else today
    return day, max(-balance_through(db, loan, day), 0)


# ------------------------------------------------------------------ due dates


def _period(schedule: list[Period], day: dt.date) -> Period:
    return in_force(schedule, day) or schedule[0]


def _on_day(day: dt.date, day_of_month: int) -> dt.date:
    return day.replace(day=min(day_of_month, calendar.monthrange(day.year, day.month)[1]))


def _step(base: dt.date, every: str, k: int, day_of_month: int | None = None) -> dt.date:
    if every == "monthly":
        moved = base + relativedelta(months=k)
        return _on_day(moved, day_of_month) if day_of_month else moved
    return base + dt.timedelta(days=k * (7 if every == "weekly" else 14))


def due_dates(
    schedule: list[Period], anchor: dt.date, after: dt.date, day_of_month: int | None = None
) -> Iterator[dt.date]:
    """The regular repayment's due dates after `after`: one period apart from `anchor`
    (a due date), monthly ones on `day_of_month` (the 31st is the 30th in a short
    month, and back to the 31st after). A new repayment at a new pace starts on its own
    date."""
    base, k, every = anchor, 0, _period(schedule, anchor).every
    dom = day_of_month or anchor.day
    while True:
        k += 1
        day = _step(base, every, k, dom)
        period = _period(schedule, day)
        since = period.since
        if (
            period.every != every
            and since is not None
            and since > _step(base, every, k - 1, dom)
        ):
            base, k, every, day, dom = since, 0, period.every, since, since.day
        if day > after:
            yield day


def usual_timing(days: list[dt.date], every: str) -> tuple[dt.date, int | None] | None:
    """(anchor, day of the month) from the last regular repayments: the last one, moved
    back to the usual weekday (a repayment held over a weekend is still due on Friday),
    and for monthly ones the usual day of the month. None without any."""
    if not days:
        return None
    recent = sorted(days)[-USUAL_DAY_FROM:]
    last = recent[-1]
    if every == "monthly":
        # A repayment on a month's last day is the 31st's (30 April, 28 February). A tie
        # goes to the day paid most recently: a new payment day wins as it comes in.
        def day_of(d: dt.date) -> int:
            return 31 if d.day == calendar.monthrange(d.year, d.month)[1] else d.day

        counts = Counter(day_of(d) for d in recent)
        latest = {day_of(d): i for i, d in enumerate(recent)}
        dom = max(counts, key=lambda d: (counts[d], latest[d]))
        # The due date the last one was for: a repayment two days early is next month's.
        nearest = (_on_day(last + relativedelta(months=m), dom) for m in (-1, 0, 1))
        return min(nearest, key=lambda d: abs((d - last).days)), dom
    counts = Counter(d.weekday() for d in recent)
    weekday = max(counts, key=lambda w: (counts[w], -((last.weekday() - w) % 7)))
    back = (last.weekday() - weekday) % 7  # Easter holds Friday's over to Tuesday
    return (last - dt.timedelta(days=back) if back <= 4 else last), None


# ------------------------------------------------------------------ simulation


@dataclass
class Terms:
    """What a projection runs on."""

    rates: list[Rate]
    schedule: list[Period]
    anchor: dt.date  # a due date: the last regular repayment, or when it began
    charge_day: int  # the day of the month interest is charged
    day_of_month: int | None = None  # monthly repayments: the usual day


@dataclass
class Scenario:
    offset_cents: float = 0.0  # in the offset accounts
    extra_cents: float = 0.0  # on top of each regular repayment
    extra_monthly: float = 0.0  # the usual extras, a month, spread over the repayments
    lump_cents: float = 0.0  # paid off at the start
    rate_change: float = 0.0  # percentage points, on every rate


@dataclass
class State:
    day: dt.date  # at the end of this day
    owing: float
    accrued: float = 0.0  # interest not yet charged


@dataclass
class Year:
    year: int
    regular_cents: float = 0.0
    extra_cents: float = 0.0
    interest_cents: float = 0.0
    owing_cents: float = 0.0


@dataclass
class Run:
    state: State
    payoff: dt.date | None = None
    interest_cents: float = 0.0
    points: list[tuple[dt.date, float]] = field(default_factory=list)
    years: list[Year] = field(default_factory=list)
    # Not paid off because a year's repayments left no less owing: they don't cover the
    # interest. (Not paid off and not stalled: it takes longer than MAX_YEARS.)
    stalls: bool = False


def _charge_day(day: dt.date, charge_day: int) -> bool:
    return day.day == min(charge_day, calendar.monthrange(day.year, day.month)[1])


def last_charge(day: dt.date, charge_day: int) -> dt.date:
    """The last day interest was charged, on or before `day`."""
    this_month = _on_day(day, charge_day)
    if this_month <= day:
        return this_month
    return _on_day(day.replace(day=1) - dt.timedelta(days=1), charge_day)


def accrued_since_charge(
    day: dt.date, owing: float, offset: float, rates: list[Rate], charge_day: int
) -> float:
    """Interest since the last charge, up to the end of `day`: a lender's balance leaves
    it out until it is charged."""
    total = 0.0
    d = last_charge(day, charge_day)
    while d < day:
        d += dt.timedelta(days=1)
        total += max(owing - offset, 0.0) * rate_on(rates, d) / 100 / 365
    return total


def simulate(
    start: State,
    terms: Terms,
    scenario: Scenario,
    stop: dt.date | None = None,
    paid: dict[dt.date, tuple[float, float]] | None = None,
) -> Run:
    """Day by day from `start` to the loan paid off -- or to `stop`, for the state then.
    `paid`: repayments already made, by day (regular, extra), applied on their days; the
    regular repayment is due after the anchor. Paid off: the day a repayment clears what
    is owed and the interest so far. Not paid off (`payoff` None): when a whole year
    leaves no less owing, or after MAX_YEARS."""
    # A lump sum pays what is owed, then the interest since the last charge.
    lump = min(scenario.lump_cents, start.owing + start.accrued)
    on_owing = min(lump, start.owing)
    owing = start.owing - on_owing
    accrued = start.accrued - (lump - on_owing)
    day = start.day
    run = Run(state=start, points=[(day, owing)], interest_cents=lump - on_owing)
    year = Year(
        (day + dt.timedelta(days=1)).year, extra_cents=lump, interest_cents=lump - on_owing
    )
    if owing <= 0 and accrued <= 0:
        run.payoff = day
        run.state = State(day, 0.0)
        run.years.append(year)
        return run
    # A year that leaves no less owing stalls the loan -- unless a rate or repayment
    # still to come changes that.
    changes = [r.since for r in terms.rates if r.since] + [
        p.since for p in terms.schedule if p.since
    ]
    settled = max(changes, default=day)
    dues = due_dates(terms.schedule, terms.anchor, day, terms.day_of_month)
    due = next(dues)
    limit = stop or day + relativedelta(years=MAX_YEARS)
    last_year_owing: float | None = None  # a whole year's repayments are compared
    while day < limit:
        day += dt.timedelta(days=1)
        rate = max(rate_on(terms.rates, day) + scenario.rate_change, 0.0)
        accrued += max(owing - scenario.offset_cents, 0.0) * rate / 100 / 365
        if _charge_day(day, terms.charge_day):
            owing += accrued
            run.interest_cents += accrued
            year.interest_cents += accrued
            accrued = 0.0
        payments: list[tuple[float, float]] = []
        if paid and day in paid:
            payments.append(paid[day])
        if day == due:
            period = _period(terms.schedule, day)
            extra = scenario.extra_cents + scenario.extra_monthly * 12 / PER_YEAR[period.every]
            payments.append((float(period.cents), extra))
            due = next(dues)
        for regular, extra in payments:
            if regular + extra >= owing + accrued:
                # The last repayment: what is left, and the interest to today.
                run.interest_cents += accrued
                year.interest_cents += accrued
                left = owing + accrued
                year.regular_cents += min(regular, left)
                year.extra_cents += max(left - regular, 0.0)
                owing = accrued = 0.0
                run.payoff = day
                break
            owing -= regular + extra
            year.regular_cents += regular
            year.extra_cents += extra
        if run.payoff:
            run.points.append((day, 0.0))
            break
        tomorrow = day + dt.timedelta(days=1)
        if tomorrow.month != day.month:
            run.points.append((day, owing))
        if tomorrow.year != day.year:
            year.owing_cents = owing
            run.years.append(year)
            year = Year(tomorrow.year)
            if (
                stop is None
                and last_year_owing is not None
                and owing >= last_year_owing
                and day >= settled
            ):
                run.stalls = True
                break
            # Compared from the first year-end after the last change, not across it.
            last_year_owing = owing if day >= settled else None
    year.owing_cents = owing
    if year.regular_cents or year.extra_cents or year.interest_cents:
        run.years.append(year)
    run.state = State(day, owing, accrued)
    return run


def months_between(a: dt.date, b: dt.date) -> int:
    """Whole months from a to b (negative when b is first)."""
    delta = relativedelta(b, a)
    return delta.years * 12 + delta.months


def needed_repayment(owing: float, rate: float, every: str, periods: int) -> int | None:
    """The regular repayment that pays `owing` off in `periods` repayments at `rate`
    (per cent a year): what a lender would set. None when no repayments are left."""
    if periods <= 0:
        return None
    r = rate / 100 / PER_YEAR[every]
    if r == 0:
        return round(owing / periods)
    return round(owing * r / (1 - (1 + r) ** -periods))


# ------------------------------------------------------------------ repayments made


@dataclass
class PaidMonth:
    month: str  # "2026-10"
    regular_cents: int
    extra_cents: int


@dataclass
class Repayment:
    day: dt.date  # when it left the paying account
    cents: int
    extra: bool
    arrived: dt.date | None  # when the loan's statement shows it; None: not imported


@dataclass
class History:
    paid: list[Repayment]  # oldest first
    months: list[PaidMonth]  # the last PAID_MONTHS, newest first
    usual_extra_cents: int  # a month: the typical month of the last few
    regular_days: list[dt.date]


def history(db: Session, loan: models.Account, today: dt.date, since: dt.date) -> History:
    """The loan's repayments from `since` (and at least the last PAID_MONTHS months)."""
    first_month = today.replace(day=1) - relativedelta(months=PAID_MONTHS - 1)
    found, _ = loan_repayments(db, loan.household_id, min(first_month, since), today)
    mine = found.get(loan.id)
    legs, flags = (mine.paid, mine.extra) if mine else ([], {})
    groups = [t.transfer_group_id for t in legs if t.transfer_group_id]
    arrived: dict[str, dt.date] = {}
    if groups:
        for group, day in db.execute(
            select(models.Transaction.transfer_group_id, models.Transaction.txn_date).where(
                models.Transaction.account_id == loan.id,
                models.Transaction.transfer_group_id.in_(groups),
                models.Transaction.amount_cents > 0,
            )
        ).tuples():
            if group:
                arrived[group] = day
    paid = [
        Repayment(
            t.txn_date,
            abs(t.amount_cents),
            flags[t.id],
            arrived.get(t.transfer_group_id) if t.transfer_group_id else None,
        )
        for t in legs
    ]
    months: dict[str, list[int]] = {
        (first_month + relativedelta(months=k)).strftime("%Y-%m"): [0, 0]
        for k in range(PAID_MONTHS)
    }
    for p in paid:
        key = p.day.strftime("%Y-%m")
        if key in months:
            months[key][1 if p.extra else 0] += p.cents
    # The usual extra: the typical month's extras over the last whole months there are
    # repayments for. A one-off lump sum is not usual; one most months is.
    this_month = today.replace(day=1)
    whole = [
        (this_month - relativedelta(months=k)).strftime("%Y-%m")
        for k in range(1, USUAL_EXTRA_MONTHS + 1)
    ]
    first_paid = min((p.day for p in paid), default=None)
    # The months the repayments seen cover: the first only if they start near its 1st.
    first_whole = None
    if first_paid:
        first_whole = first_paid if first_paid.day <= 3 else first_paid + relativedelta(
            months=1
        )
    counted = [m for m in whole if first_whole and m >= first_whole.strftime("%Y-%m")]
    extras = [months[m][1] for m in counted]
    # The typical month: at least half the months had this much extra (the lower middle,
    # so lumps in half the months aren't halved into a monthly extra). With only a month
    # or two to go by, an extra is usual only if it was paid in each.
    usual = round(statistics.median_low(extras) if len(extras) >= 3 else min(extras, default=0))
    return History(
        paid=paid,
        months=[PaidMonth(m, r, e) for m, (r, e) in sorted(months.items(), reverse=True)],
        usual_extra_cents=usual,
        regular_days=[p.day for p in paid if not p.extra],
    )


# ------------------------------------------------------------------ the page


@dataclass
class Outcome:
    payoff: dt.date | None
    interest_cents: int
    stalls: bool  # never paid off: the repayments don't cover the interest


@dataclass
class Projection:
    owing_cents: int  # today
    estimated: bool  # owing today is an estimate from `known_on`
    known_on: dt.date
    known_cents: int
    offset_cents: int
    usual_extra_cents: int  # a month
    next_due: dt.date
    next_cents: int
    every: str
    rate: float  # today's
    contract_end: dt.date | None
    needed_cents: int | None  # the repayment to finish by the contract end
    as_you_pay: Outcome
    regular_only: Outcome
    without_offset: Outcome
    # The extras paid over the last year, and the regular repayments without them.
    extras_made_cents: int
    without_extras_made: Outcome | None
    what_if: Outcome | None
    what_if_needed_cents: int | None
    points: list[dict]
    schedule: list[Year]
    what_if_schedule: list[Year] | None


@dataclass
class WhatIf:
    extra_cents: int = 0
    lump_cents: int = 0
    rate_change: float = 0.0
    offset_change_cents: int = 0

    def any(self) -> bool:
        return bool(
            self.extra_cents or self.lump_cents or self.rate_change or self.offset_change_cents
        )


def missing(mortgage: models.Mortgage, loan: models.Account) -> list[str]:
    """What the projection still needs: "rate", "repayment"."""
    needs = []
    if not rates_of(mortgage):
        needs.append("rate")
    if not schedule_of(loan):
        needs.append("repayment")
    return needs


def offset_balance(db: Session, mortgage: models.Mortgage) -> dict[str, int]:
    """Each offset account's balance, by id (as the Accounts page shows it)."""
    found: dict[str, int] = {}
    for account_id in mortgage.offset_account_ids or []:
        account = db.get(models.Account, account_id)
        if account is not None and account.household_id == mortgage.household_id:
            found[account.id] = balance_through(db, account, None)
    return found


def _outcome(run: Run) -> Outcome:
    return Outcome(run.payoff, round(run.interest_cents), run.stalls)


def _rounded(years: list[Year]) -> list[Year]:
    return [
        Year(
            y.year,
            round(y.regular_cents),
            round(y.extra_cents),
            round(y.interest_cents),
            round(max(y.owing_cents, 0)),
        )
        for y in years
    ]


def _since_known(paid: list[Repayment], known_on: dt.date, today: dt.date) -> dict:
    """The repayments made after what the books know (not yet on the loan's statement),
    by day: they come off the estimate to today."""
    by_day: dict[dt.date, tuple[float, float]] = {}
    for p in paid:
        if p.arrived is not None and p.arrived <= known_on:
            continue  # in the loan's balance already
        if p.arrived is None and p.day <= known_on:
            continue  # before the day the household said what was owed
        if p.day > today:
            continue
        day = max(p.day, known_on + dt.timedelta(days=1))
        regular, extra = by_day.get(day, (0.0, 0.0))
        by_day[day] = (regular + (0 if p.extra else p.cents), extra + (p.cents if p.extra else 0))
    return by_day


def project(
    db: Session,
    mortgage: models.Mortgage,
    loan: models.Account,
    what_if: WhatIf | None = None,
    today: dt.date | None = None,
    past: History | None = None,
) -> Projection | None:
    """The loan as it stands, and where it is going: as the household pays (with its
    usual extras), at the regular repayment only, without the offset, and with what the
    household is trying out. None until the rate and repayment are set."""
    if missing(mortgage, loan):
        return None
    today = today or dt.date.today()
    what_if = what_if or WhatIf()
    rates, schedule = rates_of(mortgage), schedule_of(loan)
    known_on, known_cents = known_owing(db, loan, today)
    offsets = max(sum(offset_balance(db, mortgage).values()), 0)
    past = past or history(db, loan, today, known_on)
    every = _period(schedule, max(today, known_on)).every
    # When the repayment is due: from the repayments at the pace in force on the last one
    # (not the weekly ones before a change to monthly, say).
    last_regular = max(past.regular_days, default=None)
    timing = None
    paced_from: dt.date | None = None  # when the pace in force began (None: always)
    if last_regular:
        pace = _period(schedule, last_regular)
        if in_force(schedule, last_regular) is not None:
            i = schedule.index(pace)
            while i > 0 and schedule[i - 1].every == pace.every:
                i -= 1  # a new amount at the same pace is the same pace
            paced_from = schedule[i].since if i > 0 else None
        timing = usual_timing(
            [d for d in past.regular_days if paced_from is None or d >= paced_from], pace.every
        )
    if timing:
        anchor, day_of_month = timing
        if last_regular and paced_from and anchor < paced_from <= last_regular:
            anchor = last_regular
    else:
        anchor = next(
            (p.since for p in reversed(schedule) if p.since and p.since <= known_on), known_on
        )
        day_of_month = None
    charge_day = charge_day_of(db, loan) or (mortgage.start_date or anchor).day
    terms = Terms(rates, schedule, anchor, charge_day, day_of_month)
    as_you_pay = Scenario(offset_cents=offsets, extra_monthly=past.usual_extra_cents)

    # To today: what the books know, the interest since its last charge, and the
    # repayments made since; then the regular repayment. From today, each way.
    start = State(
        known_on,
        float(known_cents),
        accrued_since_charge(known_on, known_cents, offsets, rates, charge_day),
    )
    # The extras made to today are the ones seen: none are assumed.
    to_today = simulate(
        start,
        terms,
        Scenario(offset_cents=offsets),
        stop=today,
        paid=_since_known(past.paid, known_on, today),
    )
    now = to_today.state
    if to_today.payoff or now.day < today:
        now = State(max(today, now.day), now.owing, now.accrued)
    runs = {
        "as_you_pay": simulate(now, terms, as_you_pay),
        "regular_only": simulate(now, terms, Scenario(offset_cents=offsets)),
        "without_offset": simulate(
            now, terms, Scenario(extra_monthly=past.usual_extra_cents)
        ),
    }
    year_ago = today - relativedelta(years=1)
    extras_made = sum(p.cents for p in past.paid if p.extra and year_ago < p.day <= today)
    without_extras = None
    if extras_made:
        before = State(now.day, now.owing + extras_made, now.accrued)
        without_extras = simulate(before, terms, Scenario(offset_cents=offsets))
    trying = None
    if what_if.any():
        trying = simulate(
            now,
            terms,
            Scenario(
                offset_cents=max(offsets + what_if.offset_change_cents, 0),
                extra_cents=what_if.extra_cents,
                extra_monthly=past.usual_extra_cents,
                lump_cents=what_if.lump_cents,
                rate_change=what_if.rate_change,
            ),
        )

    contract_end = None
    if mortgage.start_date and mortgage.term_months:
        contract_end = mortgage.start_date + relativedelta(months=mortgage.term_months)
    rate_today = rate_on(rates, now.day)

    def needed(owing: float, change: float) -> int | None:
        if contract_end is None or owing <= 0:
            return None
        dues = due_dates(schedule, anchor, now.day, day_of_month)
        left = sum(1 for _ in _until(dues, contract_end))
        return needed_repayment(owing, max(rate_today + change, 0), every, left)

    next_due = next(due_dates(schedule, anchor, now.day, day_of_month))
    return Projection(
        owing_cents=round(now.owing),
        estimated=known_on < today,
        known_on=known_on,
        known_cents=known_cents,
        offset_cents=offsets,
        usual_extra_cents=past.usual_extra_cents,
        next_due=next_due,
        next_cents=_period(schedule, next_due).cents,
        every=every,
        rate=rate_today,
        contract_end=contract_end,
        needed_cents=needed(now.owing, 0.0),
        as_you_pay=_outcome(runs["as_you_pay"]),
        regular_only=_outcome(runs["regular_only"]),
        without_offset=_outcome(runs["without_offset"]),
        extras_made_cents=extras_made,
        without_extras_made=_outcome(without_extras) if without_extras else None,
        what_if=_outcome(trying) if trying else None,
        what_if_needed_cents=(
            needed(max(now.owing - what_if.lump_cents, 0), what_if.rate_change)
            if trying and what_if.rate_change
            else None
        ),
        points=_points(
            {
                "as_you_pay": runs["as_you_pay"],
                "regular_only": runs["regular_only"],
                **({"what_if": trying} if trying else {}),
            }
        ),
        schedule=_rounded(runs["as_you_pay"].years),
        what_if_schedule=_rounded(trying.years) if trying else None,
    )


def _until(days: Iterator[dt.date], end: dt.date) -> Iterator[dt.date]:
    for day in days:
        if day > end:
            return
        yield day


def _points(runs: dict[str, Run]) -> list[dict]:
    """One row a month for the chart: what each way owes at the month's end. A loan paid
    off stays at 0 until the others are; one never paid off just ends."""
    by_month: dict[str, dict] = {}
    for name, run in runs.items():
        for day, owing in run.points:
            row = by_month.setdefault(day.strftime("%Y-%m"), {"month": day.strftime("%Y-%m")})
            row[name] = round(max(owing, 0))
    rows = [by_month[m] for m in sorted(by_month)]
    for name, run in runs.items():
        if run.payoff is None:
            continue
        seen = False
        for row in rows:
            if name in row:
                seen = True
            elif seen:
                row[name] = 0
    return rows
