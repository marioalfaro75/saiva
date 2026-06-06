"""Seed a household's default taxonomy + starter rules, and generate a realistic
demo dataset for onboarding/empty states and tests (PRD §14, Appendix D)."""

from __future__ import annotations

import datetime as dt
import random

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, security
from ..taxonomy import DEFAULT_TAXONOMY, STARTER_RULES
from .importers import dedup_hash
from .merchants import normalise_merchant
from .transfers import detect_transfers

DEMO_EMAIL = "demo@saiva.app"
DEMO_PASSWORD = "demodemodemo"  # noqa: S105 — local demo account only


def seed_household_defaults(db: Session, household: models.Household) -> None:
    """Create the default category tree and system rules (idempotent)."""
    if db.execute(
        select(models.Category.id).where(models.Category.household_id == household.id)
    ).first():
        return

    name_to_id: dict[str, str] = {}
    sort = 0
    for parent in DEFAULT_TAXONOMY:
        p = models.Category(
            household_id=household.id, name=parent["name"], kind=parent["kind"],
            is_system=True, sort=sort,
        )
        sort += 1
        db.add(p)
        db.flush()
        name_to_id[parent["name"]] = p.id
        for child in parent["children"]:
            c = models.Category(
                household_id=household.id, name=child, parent_id=p.id,
                kind=parent["kind"], is_system=True, sort=sort,
            )
            sort += 1
            db.add(c)
            db.flush()
            name_to_id[child] = c.id

    for match_type, pattern, category_name in STARTER_RULES:
        category_id = name_to_id.get(category_name)
        if category_id:
            db.add(
                models.CategorisationRule(
                    household_id=household.id, match_type=match_type, pattern=pattern,
                    category_id=category_id, priority=50, source="system",
                )
            )
    db.commit()


def _add_txn(
    db: Session,
    household_id: str,
    account_id: str,
    date: dt.date,
    amount_cents: int,
    description: str,
    category_id: str | None,
) -> int:
    db.add(
        models.Transaction(
            household_id=household_id,
            account_id=account_id,
            txn_date=date,
            amount_cents=amount_cents,
            raw_description=description,
            merchant=normalise_merchant(description),
            category_id=category_id,
            source="import",
            dedup_hash=dedup_hash(account_id, date, amount_cents, description),
        )
    )
    return 1


def create_demo_budgets(db: Session, household: models.Household) -> None:
    """A few realistic monthly budgets so the Budgets page isn't empty in the demo."""
    cats = {
        c.name: c.id
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household.id)
        )
        .scalars()
        .all()
    }
    for name, limit in [("Supermarkets", 90000), ("Takeaway", 30000), ("Coffee", 8000)]:
        category_id = cats.get(name)
        if category_id:
            db.add(
                models.Budget(
                    household_id=household.id,
                    category_id=category_id,
                    period="monthly",
                    limit_cents=limit,
                )
            )
    db.commit()


def create_demo_net_worth(db: Session, household: models.Household) -> None:
    """A demo balance sheet plus six monthly snapshots so the trend isn't flat."""
    items = [
        ("Family home", "asset", 85000000),
        ("Superannuation", "asset", 19500000),
        ("Car", "asset", 2800000),
        ("Mortgage", "liability", 52000000),
        ("Car loan", "liability", 1400000),
    ]
    for sort, (name, kind, value) in enumerate(items):
        db.add(
            models.NetWorthItem(
                household_id=household.id, name=name, kind=kind, value_cents=value, sort=sort
            )
        )
    assets = sum(v for _, k, v in items if k == "asset")
    liabilities = sum(v for _, k, v in items if k == "liability")
    first_of_month = dt.date.today().replace(day=1)
    for n in range(5, -1, -1):  # six months up to now, trending upward
        as_of = first_of_month - relativedelta(months=n)
        a = assets - n * 600000
        liabs = liabilities + n * 50000
        db.add(
            models.NetWorthSnapshot(
                household_id=household.id, as_of=as_of,
                assets_cents=a, liabilities_cents=liabs, net_cents=a - liabs,
            )
        )
    db.commit()


def create_demo_data(db: Session, household: models.Household, months: int = 6) -> int:
    everyday = models.Account(
        household_id=household.id, name="Everyday", type="everyday",
        institution="CBA", opening_balance_cents=320000,
    )
    savings = models.Account(
        household_id=household.id, name="Savings", type="savings",
        institution="CBA", opening_balance_cents=1800000,
    )
    credit = models.Account(
        household_id=household.id, name="Credit Card", type="credit_card",
        institution="Amex", opening_balance_cents=0,
    )
    db.add_all([everyday, savings, credit])
    db.flush()

    cats = {
        c.name: c.id
        for c in db.execute(
            select(models.Category).where(models.Category.household_id == household.id)
        )
        .scalars()
        .all()
    }
    rng = random.Random(42)
    today = dt.date.today()
    start = today.replace(day=1) - relativedelta(months=months - 1)
    count = 0

    salary_day = start
    while salary_day <= today:
        count += _add_txn(db, household.id, everyday.id, salary_day, 380000,
                          "DIRECT CREDIT SALARY ACME PTY LTD PAYROLL", cats.get("Salary/Wages"))
        salary_day += dt.timedelta(days=14)

    month = start
    while month <= today:
        count += _add_txn(db, household.id, everyday.id, month.replace(day=2), -265000,
                          "MORTGAGE REPAYMENT HOME LOAN", cats.get("Mortgage"))
        count += _add_txn(db, household.id, everyday.id, month.replace(day=5), -18500,
                          "AGL ELECTRICITY BPAY", cats.get("Electricity"))
        count += _add_txn(db, household.id, everyday.id, month.replace(day=6), -8990,
                          "TELSTRA MOBILE BILL", cats.get("Mobile phone"))
        count += _add_txn(db, household.id, credit.id, month.replace(day=8), -1899,
                          "NETFLIX.COM SYDNEY", cats.get("Streaming"))
        count += _add_txn(db, household.id, credit.id, month.replace(day=9), -1399,
                          "SPOTIFY P12345", cats.get("Streaming"))
        transfer_day = month.replace(day=16)
        count += _add_txn(db, household.id, everyday.id, transfer_day, -100000,
                          "TRANSFER TO SAVINGS", cats.get("Internal transfers"))
        count += _add_txn(db, household.id, savings.id, transfer_day, 100000,
                          "TRANSFER FROM EVERYDAY", cats.get("Internal transfers"))
        month += relativedelta(months=1)

    grocers = ["WOOLWORTHS 1234 SYDNEY", "COLES 4567 PARRAMATTA", "ALDI STORES 88"]
    coffees = ["CAMPOS COFFEE", "THE COFFEE CLUB", "STARBUCKS CHATSWOOD"]
    takeaways = ["UBER EATS", "MENULOG", "MCDONALDS 123"]
    day = start
    while day <= today:
        count += _add_txn(db, household.id, credit.id, day, -rng.randint(8000, 22000),
                          rng.choice(grocers), cats.get("Supermarkets"))
        if rng.random() < 0.7:
            count += _add_txn(db, household.id, credit.id, day + dt.timedelta(days=1),
                              -rng.randint(450, 750), rng.choice(coffees), cats.get("Coffee"))
        if rng.random() < 0.4:
            count += _add_txn(db, household.id, credit.id, day + dt.timedelta(days=2),
                              -rng.randint(2500, 6500), rng.choice(takeaways), cats.get("Takeaway"))
        if rng.random() < 0.25:
            count += _add_txn(
                db, household.id, credit.id, day + dt.timedelta(days=3),
                -rng.randint(3000, 15000), "BUNNINGS WAREHOUSE", cats.get("Homewares"),
            )
        day += dt.timedelta(days=7)

    db.commit()
    detect_transfers(db, household.id)
    create_demo_budgets(db, household)
    create_demo_net_worth(db, household)
    return count


def setup_demo(db: Session) -> tuple[models.User, models.Household, str]:
    existing = db.execute(
        select(models.User).where(models.User.email == DEMO_EMAIL)
    ).scalar_one_or_none()
    if existing:
        household = db.get(models.Household, existing.household_id)
        assert household is not None
        return existing, household, DEMO_PASSWORD

    household = models.Household(name="Demo Household", state="NSW", period_basis="calendar")
    db.add(household)
    db.flush()
    seed_household_defaults(db, household)
    user = models.User(
        household_id=household.id, email=DEMO_EMAIL, name="Demo Owner", role="owner",
        password_hash=security.hash_password(DEMO_PASSWORD),
    )
    db.add(user)
    db.flush()
    create_demo_data(db, household)
    db.commit()
    return user, household, DEMO_PASSWORD


def main() -> None:
    from ..db import SessionLocal
    from ..dbinit import main as ensure_schema

    ensure_schema()
    db = SessionLocal()
    try:
        user, _household, password = setup_demo(db)
        print(f"Saiva demo ready — login: {user.email} / {password}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
