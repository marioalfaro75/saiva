"""The Mortgage page (0.19.0): what is owed on a home loan, when it will be paid off, and
what extra repayments, the offset and a change of rate do to that.

Interest is worked out the way Australian lenders do it: each day, on what is owed less
what is in the offset accounts, at the rate that day; and charged once a month. The
regular repayment is the one set on the loan's account (0.18.1), on its due dates from
the last one paid. Extras are on top.

What is owed comes from the loan's account: its balance is what is owed (a loan account
reads negative). The household says what it is on a date when it sets the loan up, and a
loan statement imported after that keeps it right. From the last of those to today it is
an estimate, at the repayments set.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from .repayments import Period, in_force, loan_repayments, schedule_of

MAX_YEARS = 50  # not paid off by then: it never will be, at these repayments
USUAL_EXTRA_MONTHS = 6  # the usual extras: what was paid over this many months
PAID_MONTHS = 12  # the repayments listed, by month
PER_YEAR = {"weekly": 52, "fortnightly": 26, "monthly": 12}
MAX_RATE = 30.0


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


# ------------------------------------------------------------------ balances


def balance_through(db: Session, account: models.Account, day: dt.date | None) -> int:
    """The account's balance at the end of a day (every transaction, without one)."""
    q = select(func.coalesce(func.sum(models.Transaction.amount_cents), 0)).where(
        models.Transaction.account_id == account.id,
        models.Transaction.split_parent_id.is_(None),
    )
    if day is not None:
        q = q.where(models.Transaction.txn_date <= day)
    return account.opening_balance_cents + int(db.execute(q).scalar() or 0)


def set_balance(db: Session, account: models.Account, cents: int, on: dt.date) -> None:
    """Make the account read `cents` at the end of `on`, by its opening balance."""
    moved = balance_through(db, account, on) - account.opening_balance_cents
    account.opening_balance_cents = cents - moved


def _last_txn_date(db: Session, account: models.Account, today: dt.date) -> dt.date | None:
    return db.execute(
        select(func.max(models.Transaction.txn_date)).where(
            models.Transaction.account_id == account.id,
            models.Transaction.split_parent_id.is_(None),
            models.Transaction.txn_date <= today,
        )
    ).scalar()


def known_owing(
    db: Session, mortgage: models.Mortgage, loan: models.Account, today: dt.date
) -> tuple[dt.date, int]:
    """(day, owed): the latest day the books say what is owed -- the day the household
    said, or the end of the loan's statement after it."""
    days = [
        d
        for d in (mortgage.balance_as_of, _last_txn_date(db, loan, today))
        if d is not None and d <= today
    ]
    day = max(days) if days else today
    return day, max(-balance_through(db, loan, day), 0)


# ------------------------------------------------------------------ repayments


def _period(schedule: list[Period], day: dt.date) -> Period:
    return in_force(schedule, day) or schedule[0]


def _step(base: dt.date, every: str, k: int) -> dt.date:
    if every == "monthly":
        return base + relativedelta(months=k)
    return base + dt.timedelta(days=k * (7 if every == "weekly" else 14))


def due_dates(schedule: list[Period], anchor: dt.date, after: dt.date) -> Iterator[dt.date]:
    """The regular repayment's due dates after `after`: one period apart from `anchor`
    (the last one paid). A new repayment at a new pace starts on its own date."""
    base, k, every = anchor, 0, _period(schedule, anchor).every
    while True:
        k += 1
        day = _step(base, every, k)
        period = _period(schedule, day)
        since = period.since
        if period.every != every and since is not None and since > _step(base, every, k - 1):
            base, k, every, day = since, 0, period.every, since
        if day > after:
            yield day


# ------------------------------------------------------------------ simulation


@dataclass
class Terms:
    """What a projection runs on."""

    rates: list[Rate]
    schedule: list[Period]
    anchor: dt.date  # a due date: the last regular repayment, or when it began
    charge_day: int  # the day of the month interest is charged


@dataclass
class Scenario:
    offset_cents: float = 0.0  # in the offset accounts
    extra_cents: float = 0.0  # on top of each regular repayment
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


def _charge_day(day: dt.date, charge_day: int) -> bool:
    return day.day == min(charge_day, calendar.monthrange(day.year, day.month)[1])


def simulate(start: State, terms: Terms, scenario: Scenario, stop: dt.date | None = None) -> Run:
    """Day by day from `start` to the loan paid off -- or to `stop`, for the state then.
    Paid off: the day a repayment clears what is owed and the interest so far. Not paid
    off (`payoff` None): when a year ends owing no less than the one before, or after
    MAX_YEARS."""
    owing = max(start.owing - scenario.lump_cents, 0.0)
    accrued = start.accrued if owing > 0 else 0.0
    day = start.day
    run = Run(state=start, points=[(day, owing)])
    if owing <= 0:
        run.payoff = day
        run.state = State(day, 0.0)
        return run
    dues = due_dates(terms.schedule, terms.anchor, after=day)
    due = next(dues)
    limit = stop or day + relativedelta(years=MAX_YEARS)
    year = Year(day.year)
    # Owed at the end of the last year: a whole year of repayments that leaves no less
    # owing never pays it off. (The first year can be a few days.)
    last_year_owing: float | None = None
    while day < limit:
        day += dt.timedelta(days=1)
        rate = max(rate_on(terms.rates, day) + scenario.rate_change, 0.0)
        accrued += max(owing - scenario.offset_cents, 0.0) * rate / 100 / 365
        if _charge_day(day, terms.charge_day):
            owing += accrued
            run.interest_cents += accrued
            year.interest_cents += accrued
            accrued = 0.0
        if day == due:
            regular = float(_period(terms.schedule, day).cents)
            if regular + scenario.extra_cents >= owing + accrued:
                # The last repayment: what is left, and the interest to today.
                run.interest_cents += accrued
                year.interest_cents += accrued
                left = owing + accrued
                year.regular_cents += min(regular, left)
                year.extra_cents += max(left - regular, 0.0)
                owing = accrued = 0.0
                run.payoff = day
                run.points.append((day, 0.0))
                break
            owing -= regular + scenario.extra_cents
            year.regular_cents += regular
            year.extra_cents += scenario.extra_cents
            due = next(dues)
        tomorrow = day + dt.timedelta(days=1)
        if tomorrow.month != day.month:
            run.points.append((day, owing))
        if tomorrow.year != day.year:
            year.owing_cents = owing
            run.years.append(year)
            year = Year(tomorrow.year)
            if stop is None and last_year_owing is not None and owing >= last_year_owing:
                break  # the repayments don't cover the interest
            last_year_owing = owing
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


# ------------------------------------------------------------------ the page


@dataclass
class PaidMonth:
    month: str  # "2026-10"
    regular_cents: int
    extra_cents: int


@dataclass
class Outcome:
    payoff: dt.date | None
    interest_cents: int


@dataclass
class Projection:
    owing_cents: int  # today
    estimated: bool  # owing today is an estimate from `known_on`
    known_on: dt.date
    known_cents: int
    offset_cents: int
    usual_extra_cents: int  # a month, over the last few months
    next_due: dt.date
    next_cents: int
    every: str
    rate: float  # today's
    contract_end: dt.date | None
    needed_cents: int | None  # the repayment to finish by the contract end
    as_you_pay: Outcome
    regular_only: Outcome
    without_offset: Outcome
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


def paid_by_month(
    db: Session, loan: models.Account, today: dt.date
) -> tuple[list[PaidMonth], int, dt.date | None]:
    """The loan's repayments over the last months, regular and extra, newest first; the
    usual extra a month; and the last regular repayment's date."""
    first_month = today.replace(day=1) - relativedelta(months=PAID_MONTHS - 1)
    found, _ = loan_repayments(db, loan.household_id, first_month, today)
    mine = found.get(loan.id)
    months: dict[str, list[int]] = {}
    for k in range(PAID_MONTHS):
        months[(first_month + relativedelta(months=k)).strftime("%Y-%m")] = [0, 0]
    last_regular: dt.date | None = None
    extra_since = today - relativedelta(months=USUAL_EXTRA_MONTHS)
    extras, first_paid = 0, None
    paid, flags = (mine.paid, mine.extra) if mine else ([], {})
    for t in paid:
        is_extra = flags[t.id]
        key = t.txn_date.strftime("%Y-%m")
        if key in months:
            months[key][1 if is_extra else 0] += abs(t.amount_cents)
        if not is_extra:
            last_regular = t.txn_date
        if t.txn_date >= extra_since:
            first_paid = first_paid or t.txn_date
            if is_extra:
                extras += abs(t.amount_cents)
    # Over the months there are repayments for: a loan in Saiva for two months isn't
    # averaged over six.
    usual = 0
    if first_paid is not None:
        span = max(months_between(first_paid, today) + 1, 1)
        usual = round(extras / min(span, USUAL_EXTRA_MONTHS))
    rows = [PaidMonth(m, r, e) for m, (r, e) in sorted(months.items(), reverse=True)]
    return rows, usual, last_regular


def _outcome(run: Run) -> Outcome:
    return Outcome(run.payoff, round(run.interest_cents))


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


def project(
    db: Session,
    mortgage: models.Mortgage,
    loan: models.Account,
    what_if: WhatIf | None = None,
    today: dt.date | None = None,
    paid: tuple[list[PaidMonth], int, dt.date | None] | None = None,
) -> Projection | None:
    """The loan as it stands, and where it is going: as the household pays (with its
    usual extras), at the regular repayment only, without the offset, and with what the
    household is trying out. None until the rate and repayment are set."""
    if missing(mortgage, loan):
        return None
    today = today or dt.date.today()
    what_if = what_if or WhatIf()
    rates, schedule = rates_of(mortgage), schedule_of(loan)
    known_on, known_cents = known_owing(db, mortgage, loan, today)
    offsets = max(sum(offset_balance(db, mortgage).values()), 0)
    _, usual_monthly, last_regular = paid or paid_by_month(db, loan, today)
    every = _period(schedule, today).every
    usual_each = usual_monthly * 12 / PER_YEAR[every]
    anchor = last_regular or next(
        (p.since for p in reversed(schedule) if p.since and p.since <= known_on), known_on
    )
    charge_day = (mortgage.start_date or anchor).day
    terms = Terms(rates, schedule, anchor, charge_day)
    as_you_pay = Scenario(offset_cents=offsets, extra_cents=usual_each)

    # To today, as the household pays; from today, each way.
    now = simulate(State(known_on, float(known_cents)), terms, as_you_pay, stop=today).state
    if now.day < today:  # paid off before today
        now = State(today, 0.0)
    runs = {
        "as_you_pay": simulate(now, terms, as_you_pay),
        "regular_only": simulate(now, terms, Scenario(offset_cents=offsets)),
        "without_offset": simulate(now, terms, Scenario(extra_cents=usual_each)),
    }
    trying = None
    if what_if.any():
        trying = simulate(
            now,
            terms,
            Scenario(
                offset_cents=max(offsets + what_if.offset_change_cents, 0),
                extra_cents=usual_each + what_if.extra_cents,
                lump_cents=what_if.lump_cents,
                rate_change=what_if.rate_change,
            ),
        )

    contract_end = None
    if mortgage.start_date and mortgage.term_months:
        contract_end = mortgage.start_date + relativedelta(months=mortgage.term_months)
    rate_today = rate_on(rates, today)

    def needed(owing: float, change: float) -> int | None:
        if contract_end is None or owing <= 0:
            return None
        left = sum(1 for _ in _until(due_dates(schedule, anchor, today), contract_end))
        return needed_repayment(owing, max(rate_today + change, 0), every, left)

    next_due = next(due_dates(schedule, anchor, today))
    return Projection(
        owing_cents=round(now.owing),
        estimated=known_on < today,
        known_on=known_on,
        known_cents=known_cents,
        offset_cents=offsets,
        usual_extra_cents=usual_monthly,
        next_due=next_due,
        next_cents=_period(schedule, next_due).cents,
        every=every,
        rate=rate_today,
        contract_end=contract_end,
        needed_cents=needed(now.owing, 0.0),
        as_you_pay=_outcome(runs["as_you_pay"]),
        regular_only=_outcome(runs["regular_only"]),
        without_offset=_outcome(runs["without_offset"]),
        what_if=_outcome(trying) if trying else None,
        what_if_needed_cents=(
            needed(max(now.owing - what_if.lump_cents, 0), what_if.rate_change)
            if trying
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
    """One row a month for the chart: what each way owes at the month's end (0 once paid
    off, nothing after the last of them is)."""
    by_month: dict[str, dict] = {}
    for name, run in runs.items():
        for day, owing in run.points:
            row = by_month.setdefault(day.strftime("%Y-%m"), {"month": day.strftime("%Y-%m")})
            row[name] = round(max(owing, 0))
    rows = [by_month[m] for m in sorted(by_month)]
    for name in runs:  # a line that has ended stays at 0 until the others end
        seen = False
        for row in rows:
            if name in row:
                seen = True
            elif seen:
                row[name] = 0
    return rows
