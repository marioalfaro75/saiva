"""Alerts (PRD R35) and email digests (R34).

Alerts are derived from existing data — budgets, the insight feed, upcoming bills,
large transactions and the cashflow forecast — and stored idempotently keyed by a
stable `key`, so regenerating never duplicates them. The cron entrypoint also
emails new alerts and sends due digests. All email is opt-in and quiet by default.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from . import forecast as forecast_service
from . import recurring as recurring_service
from .budgets import list_budgets
from .insights import generate as generate_insights
from .mailer import send_email

LARGE_TXN_LOOKBACK_DAYS = 7
BILL_LOOKAHEAD_DAYS = 7
LOW_BALANCE_HORIZON_DAYS = 30
INSIGHT_ALERT_TYPES = {"mover", "creep", "fee", "duplicate"}


@dataclass
class Candidate:
    key: str
    type: str
    severity: str
    title: str
    body: str
    link: str | None = None
    amount_cents: int | None = None


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def settings_for(db: Session, household_id: str) -> models.NotificationSettings:
    ns = db.get(models.NotificationSettings, household_id)
    if ns is None:
        ns = models.NotificationSettings(household_id=household_id)
        db.add(ns)
        db.commit()
        db.refresh(ns)
    return ns


def _recipients(db: Session, household_id: str) -> list[str]:
    return list(
        db.execute(
            select(models.User.email).where(models.User.household_id == household_id)
        ).scalars()
    )


def _candidates(
    db: Session, household: models.Household, today: dt.date, ns: models.NotificationSettings
) -> list[Candidate]:
    out: list[Candidate] = []

    for b in list_budgets(db, household, today=today):
        if b.status == "over":
            out.append(
                Candidate(
                    key=f"budget_over:{b.id}:{b.period_start}",
                    type="budget",
                    severity="alert",
                    title=f"Over budget: {b.category_name}",
                    body=f"{_money(b.actual_cents)} of {_money(b.limit_cents)} "
                    f"spent on {b.category_name} this {b.period}.",
                    link="/budgets",
                    amount_cents=b.actual_cents - b.limit_cents,
                )
            )
        elif b.status == "warning":
            out.append(
                Candidate(
                    key=f"budget_warn:{b.id}:{b.period_start}",
                    type="budget",
                    severity="warn",
                    title=f"Nearing budget: {b.category_name}",
                    body=f"{_money(b.actual_cents)} of {_money(b.limit_cents)} "
                    f"used on {b.category_name} this {b.period}.",
                    link="/budgets",
                    amount_cents=b.actual_cents,
                )
            )

    for bill in recurring_service.upcoming_bills(
        db, household.id, days=BILL_LOOKAHEAD_DAYS, today=today
    ):
        out.append(
            Candidate(
                key=f"bill:{bill.merchant}:{bill.due_date}",
                type="bill",
                severity="info",
                title=f"Bill due: {bill.merchant}",
                body=f"{bill.merchant} ~{_money(bill.amount_cents)} due {bill.due_date}.",
                link="/bills",
                amount_cents=bill.amount_cents,
            )
        )

    large = (
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household.id,
                models.Transaction.txn_date >= today - dt.timedelta(days=LARGE_TXN_LOOKBACK_DAYS),
                models.Transaction.amount_cents <= -ns.large_txn_threshold_cents,
                models.Transaction.is_transfer.is_(False),
                models.Transaction.split_parent_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for t in large:
        out.append(
            Candidate(
                key=f"large_txn:{t.id}",
                type="large_txn",
                severity="info",
                title="Large transaction",
                body=f"{t.merchant or t.raw_description} "
                f"{_money(-t.amount_cents)} on {t.txn_date}.",
                link="/transactions",
                amount_cents=-t.amount_cents,
            )
        )

    f = forecast_service.forecast(db, household.id, days=LOW_BALANCE_HORIZON_DAYS, today=today)
    if f.low_balance_cents < ns.low_balance_threshold_cents:
        out.append(
            Candidate(
                key=f"low_balance:{f.low_balance_date}",
                type="low_balance",
                severity="alert" if f.low_balance_cents < 0 else "warn",
                title="Low balance ahead",
                body=f"Projected balance dips to {_money(f.low_balance_cents)} "
                f"around {f.low_balance_date}.",
                link="/forecast",
                amount_cents=f.low_balance_cents,
            )
        )

    for ins in generate_insights(db, household, today=today).insights:
        if ins.severity in ("alert", "warn") and ins.type in INSIGHT_ALERT_TYPES:
            out.append(
                Candidate(
                    key=f"insight:{ins.key}",
                    type="unusual_spend",
                    severity=ins.severity,
                    title=ins.title,
                    body=ins.body,
                    link=ins.link,
                    amount_cents=ins.amount_cents,
                )
            )
    return out


def generate(
    db: Session, household: models.Household, today: dt.date | None = None
) -> list[models.Notification]:
    """Idempotently create notifications for any new candidates; return new ones."""
    today = today or dt.date.today()
    ns = settings_for(db, household.id)
    existing = set(
        db.execute(
            select(models.Notification.key).where(
                models.Notification.household_id == household.id
            )
        ).scalars()
    )
    created: list[models.Notification] = []
    for c in _candidates(db, household, today, ns):
        if c.key in existing:
            continue
        existing.add(c.key)
        note = models.Notification(
            household_id=household.id,
            key=c.key,
            type=c.type,
            severity=c.severity,
            title=c.title,
            body=c.body,
            link=c.link,
            amount_cents=c.amount_cents,
        )
        db.add(note)
        created.append(note)
    if created:
        db.commit()
        for note in created:
            db.refresh(note)
    return created


def _digest_due(ns: models.NotificationSettings, today: dt.date) -> bool:
    if ns.digest not in ("weekly", "monthly"):
        return False
    if ns.last_digest_at is None:
        return True
    period = 7 if ns.digest == "weekly" else 30
    return (today - ns.last_digest_at.date()).days >= period


def _digest_body(db: Session, household: models.Household, today: dt.date) -> str:
    f = forecast_service.forecast(db, household.id, days=30, today=today)
    bills = recurring_service.upcoming_bills(db, household.id, days=14, today=today)
    over = [b for b in list_budgets(db, household, today=today) if b.status == "over"]
    lines = [
        f"Your Saiva summary for {today:%d %b %Y}",
        "",
        f"Balance today: {_money(f.starting_balance_cents)}",
        f"Projected in 30 days: {_money(f.end_balance_cents)} "
        f"(low {_money(f.low_balance_cents)} around {f.low_balance_date})",
        "",
        f"Budgets over limit: {len(over)}",
        f"Bills due in the next 14 days: {len(bills)} "
        f"({_money(sum(b.amount_cents for b in bills))})",
    ]
    return "\n".join(lines)


def _alerts_body(notes: list[models.Notification]) -> str:
    lines = ["New alerts from Saiva:", ""]
    lines += [f"- [{n.severity}] {n.title} — {n.body}" for n in notes]
    return "\n".join(lines)


def run_for_household(
    db: Session, household: models.Household, today: dt.date | None = None
) -> dict[str, int]:
    """Generate alerts and (if enabled) email new ones plus any due digest."""
    today = today or dt.date.today()
    ns = settings_for(db, household.id)
    created = generate(db, household, today=today)
    emailed = 0
    digest_sent = 0

    if ns.email_enabled and get_settings().smtp_configured:
        recipients = _recipients(db, household.id)
        urgent = [n for n in created if n.severity in ("alert", "warn") and n.emailed_at is None]
        if urgent and recipients and send_email(
            recipients, f"{len(urgent)} new alert(s) from Saiva", _alerts_body(urgent)
        ):
            now = dt.datetime.utcnow()
            for n in urgent:
                n.emailed_at = now
            emailed = len(urgent)
            db.commit()
        if _digest_due(ns, today) and recipients and send_email(
            recipients, f"Your {ns.digest} Saiva summary", _digest_body(db, household, today)
        ):
            ns.last_digest_at = dt.datetime.utcnow()
            db.commit()
            digest_sent = 1

    return {"created": len(created), "emailed": emailed, "digest_sent": digest_sent}


def run_all(db: Session, today: dt.date | None = None) -> dict[str, int]:
    totals = {"households": 0, "created": 0, "emailed": 0, "digests": 0}
    for household in db.execute(select(models.Household)).scalars():
        r = run_for_household(db, household, today=today)
        totals["households"] += 1
        totals["created"] += r["created"]
        totals["emailed"] += r["emailed"]
        totals["digests"] += r["digest_sent"]
    return totals
