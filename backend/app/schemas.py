"""Pydantic request/response models."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# --------------------------------------------------------------------------- auth


class SetupRequest(BaseModel):
    household_name: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    state: str | None = None
    period_basis: Literal["calendar", "weekly", "fortnightly", "monthly"] = "calendar"


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: EmailStr
    name: str
    role: str
    household_id: str


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    role: Literal["owner", "adult", "viewer"] = "adult"


# ----------------------------------------------------------------------- household


class HouseholdOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    country: str
    currency: str
    locale: str
    state: str | None
    timezone: str
    fy_start_month: int
    fy_start_day: int
    period_basis: str
    pay_cycle_anchor: dt.date | None
    adults: int
    children: int
    income_band: str | None


class HouseholdUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    state: str | None = None
    currency: str | None = Field(default=None, max_length=3)
    locale: str | None = Field(default=None, max_length=10)
    timezone: str | None = None
    fy_start_month: int | None = Field(default=None, ge=1, le=12)
    fy_start_day: int | None = Field(default=None, ge=1, le=31)
    period_basis: Literal["calendar", "weekly", "fortnightly", "monthly"] | None = None
    pay_cycle_anchor: dt.date | None = None
    adults: int | None = Field(default=None, ge=0)
    children: int | None = Field(default=None, ge=0)
    income_band: str | None = None


class MeOut(BaseModel):
    user: UserOut
    household: HouseholdOut
    csrf_token: str


# ------------------------------------------------------------------------ accounts


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: Literal[
        "everyday", "savings", "credit_card", "home_loan",
        "offset", "personal_loan", "cash", "investment",
    ] = "everyday"
    institution: str | None = None
    currency: str = "AUD"
    opening_balance_cents: int = 0
    owner_user_id: str | None = None


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    type: str | None = None
    institution: str | None = None
    opening_balance_cents: int | None = None
    owner_user_id: str | None = None


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    type: str
    institution: str | None
    currency: str
    opening_balance_cents: int
    owner_user_id: str | None
    balance_cents: int = 0
    txn_count: int = 0


# ---------------------------------------------------------------------- categories


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    parent_id: str | None
    kind: str
    icon: str | None
    color: str | None
    is_system: bool
    sort: int


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    parent_id: str | None = None
    kind: Literal["income", "expense", "transfer", "savings"] = "expense"
    icon: str | None = None
    color: str | None = None


# --------------------------------------------------------------------------- rules


class RuleCreate(BaseModel):
    match_type: Literal["contains", "starts_with", "regex", "merchant"] = "contains"
    pattern: str = Field(min_length=1, max_length=200)
    category_id: str
    priority: int = 100


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    match_type: str
    pattern: str
    category_id: str
    priority: int
    source: str
    is_active: bool


# ------------------------------------------------------------------------- imports


class CsvMapping(BaseModel):
    has_header: bool = True
    date_col: int
    description_col: int
    amount_mode: Literal["single", "debit_credit"] = "single"
    amount_col: int | None = None
    debit_col: int | None = None
    credit_col: int | None = None
    balance_col: int | None = None
    date_format: str | None = None  # e.g. "%d/%m/%Y"; auto-detected if omitted
    decimal: str = "."
    invert_amount: bool = False  # set if outflows are positive in the file
    skip_rows: int = 0


class ImportSniffOut(BaseModel):
    detected_format: str
    has_header: bool
    columns: list[str]
    sample_rows: list[list[str]]
    suggested_mapping: CsvMapping | None


class PreviewRow(BaseModel):
    txn_date: dt.date
    amount_cents: int
    raw_description: str
    merchant: str | None
    suggested_category_id: str | None
    suggested_category_name: str | None
    confidence: float | None
    is_duplicate: bool


class ImportPreviewOut(BaseModel):
    account_id: str
    file_format: str
    total_rows: int
    new_rows: list[PreviewRow]
    duplicate_count: int


class ImportCommitOut(BaseModel):
    batch_id: str
    added: int
    skipped: int
    transfers_linked: int


# -------------------------------------------------------------------- transactions


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    account_id: str
    account_name: str | None = None
    txn_date: dt.date
    amount_cents: int
    raw_description: str
    merchant: str | None
    category_id: str | None
    category_name: str | None = None
    is_transfer: bool
    is_recurring: bool
    confidence: float | None
    source: str
    notes: str | None
    tags: list[str]
    split_parent_id: str | None


class TransactionListOut(BaseModel):
    items: list[TransactionOut]
    total: int
    page: int
    page_size: int


class TransactionUpdate(BaseModel):
    merchant: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    is_transfer: bool | None = None


class RecategoriseRequest(BaseModel):
    category_id: str | None
    apply_to_similar: bool = False
    make_rule: bool = False


class ManualTxnCreate(BaseModel):
    account_id: str
    txn_date: dt.date
    amount_cents: int
    description: str = Field(min_length=1, max_length=400)
    category_id: str | None = None
    notes: str | None = None
    tags: list[str] = Field(default_factory=list)


class SplitItem(BaseModel):
    amount_cents: int
    category_id: str | None = None
    notes: str | None = None


class SplitRequest(BaseModel):
    splits: list[SplitItem] = Field(min_length=2)


# ----------------------------------------------------------------------- dashboard


class SummaryOut(BaseModel):
    period_label: str
    start: dt.date
    end: dt.date
    income_cents: int
    expense_cents: int
    net_cents: int
    savings_rate: float
    txn_count: int


class CategoryBreakdownItem(BaseModel):
    category_id: str | None
    category_name: str
    parent_name: str | None
    kind: str
    amount_cents: int
    pct: float


class CategoryBreakdownOut(BaseModel):
    start: dt.date
    end: dt.date
    total_cents: int
    items: list[CategoryBreakdownItem]


class TrendPoint(BaseModel):
    period_start: dt.date
    income_cents: int
    expense_cents: int
    net_cents: int


class TrendOut(BaseModel):
    interval: str
    points: list[TrendPoint]


class MessageOut(BaseModel):
    message: str
