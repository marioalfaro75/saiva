"""SQLAlchemy ORM models (PRD §13). Monetary values are stored as signed integer
cents to avoid floating-point error; a negative amount is money out."""

from __future__ import annotations

import datetime as dt
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid4().hex


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow, nullable=False
    )


class Household(Base, TimestampMixin):
    __tablename__ = "households"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    country: Mapped[str] = mapped_column(String(2), default="AU")
    currency: Mapped[str] = mapped_column(String(3), default="AUD")
    locale: Mapped[str] = mapped_column(String(10), default="en-AU")
    state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Australia/Sydney")

    # Financial year (default 1 July – 30 June) and budget-period basis (PRD R1, §9).
    fy_start_month: Mapped[int] = mapped_column(Integer, default=7)
    fy_start_day: Mapped[int] = mapped_column(Integer, default=1)
    period_basis: Mapped[str] = mapped_column(String(12), default="calendar")  # see PERIOD_BASES
    pay_cycle_anchor: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # Optional, used for ABS benchmarking later.
    adults: Mapped[int] = mapped_column(Integer, default=1)
    children: Mapped[int] = mapped_column(Integer, default=0)
    income_band: Mapped[str | None] = mapped_column(String(32), nullable=True)

    users: Mapped[list[User]] = relationship(back_populates="household")


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), nullable=False)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(12), default="owner")  # owner|adult|viewer
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    household: Mapped[Household] = relationship(back_populates="users")


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    institution: Mapped[str | None] = mapped_column(String(120), nullable=True)
    type: Mapped[str] = mapped_column(String(20), default="everyday")  # see ACCOUNT_TYPES
    currency: Mapped[str] = mapped_column(String(3), default="AUD")
    opening_balance_cents: Mapped[int] = mapped_column(Integer, default=0)
    owner_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Category(Base, TimestampMixin):
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), default="expense")  # see CATEGORY_KINDS
    abs_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(40), nullable=True)
    color: Mapped[str | None] = mapped_column(String(9), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class CategorisationRule(Base, TimestampMixin):
    __tablename__ = "categorisation_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    match_type: Mapped[str] = mapped_column(String(12), default="contains")  # see MATCH_TYPES
    pattern: Mapped[str] = mapped_column(String(200), nullable=False)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    source: Mapped[str] = mapped_column(String(8), default="user")  # user|system
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_format: Mapped[str] = mapped_column(String(8), default="csv")  # csv|ofx|qfx|qif
    status: Mapped[str] = mapped_column(String(10), default="committed")  # committed|undone
    mapping_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    added_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Transaction(Base, TimestampMixin):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    txn_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)  # signed; <0 = outflow
    raw_description: Mapped[str] = mapped_column(Text, default="")
    merchant: Mapped[str | None] = mapped_column(String(160), nullable=True)
    category_id: Mapped[str | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    is_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    transfer_group_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(8), default="import")  # import|manual
    dedup_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    import_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("import_batches.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    split_parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("transactions.id"), nullable=True
    )

    __table_args__ = (
        Index("ix_txn_account_dedup", "account_id", "dedup_hash"),
        Index("ix_txn_household_date", "household_id", "txn_date"),
    )


class Budget(Base, TimestampMixin):
    """A flexible per-category spending limit for a recurring period (PRD R24)."""

    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    category_id: Mapped[str] = mapped_column(ForeignKey("categories.id"), nullable=False)
    period: Mapped[str] = mapped_column(String(12), default="monthly")  # see BUDGET_PERIODS
    limit_cents: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("household_id", "category_id", name="uq_budget_household_category"),
    )


class NetWorthItem(Base, TimestampMixin):
    """A manually tracked asset or liability (PRD net worth, Phase 2)."""

    __tablename__ = "net_worth_items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), default="asset")  # asset | liability
    value_cents: Mapped[int] = mapped_column(Integer, default=0)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class NetWorthSnapshot(Base, TimestampMixin):
    """A point-in-time net-worth total for the trend chart (one row per day)."""

    __tablename__ = "net_worth_snapshots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    as_of: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    assets_cents: Mapped[int] = mapped_column(Integer, default=0)
    liabilities_cents: Mapped[int] = mapped_column(Integer, default=0)
    net_cents: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        UniqueConstraint("household_id", "as_of", name="uq_networth_snapshot_day"),
    )


class SavingsGoal(Base, TimestampMixin):
    """A savings target with an optional deadline and linked account (PRD R25)."""

    __tablename__ = "savings_goals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    household_id: Mapped[str] = mapped_column(ForeignKey("households.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    target_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    target_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    current_cents: Mapped[int] = mapped_column(Integer, default=0)
    sort: Mapped[int] = mapped_column(Integer, default=0)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, index=True
    )
    household_id: Mapped[str | None] = mapped_column(ForeignKey("households.id"), nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    entity: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
