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


_MatchType = Literal["contains", "starts_with", "regex", "merchant", "equals"]


class RuleCreate(BaseModel):
    match_type: _MatchType = "contains"
    pattern: str = Field(min_length=1, max_length=200)
    category_id: str
    priority: int = 10  # user rules win over system starter rules (priority 50)


class RuleUpdate(BaseModel):
    match_type: _MatchType | None = None
    pattern: str | None = Field(default=None, min_length=1, max_length=200)
    category_id: str | None = None
    priority: int | None = None
    is_active: bool | None = None


class RulePreviewRequest(BaseModel):
    match_type: _MatchType = "contains"
    pattern: str = Field(min_length=1, max_length=200)


class RulePreviewOut(BaseModel):
    matched: int  # all transactions the rule matches
    fillable: int  # of those, how many are blank + unlocked (a backfill would set)
    samples: list[str]


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
    category_locked: bool = False
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
    category_locked: bool | None = None


class RecategoriseRequest(BaseModel):
    category_id: str | None
    # Apply the same category to look-alikes: by normalised merchant, exact raw
    # description, or a free-text "contains" match. "none" touches only this row.
    scope: Literal["none", "merchant", "exact", "contains"] = "none"
    pattern: str | None = None  # text for scope="contains" (and the rule, if made)
    make_rule: bool = False  # also persist a rule so future imports auto-apply
    lock: bool = False  # exempt the affected rows from future auto-categorisation


class RecategoriseOut(BaseModel):
    transaction: TransactionOut
    updated_count: int  # rows changed, including this one


class BulkCategoriseRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=1000)
    category_id: str | None = None
    set_category: bool = True  # when False, only the lock is changed (category untouched)
    lock: bool | None = None  # set/clear the auto-categorisation lock if provided


class CountOut(BaseModel):
    updated: int


class TxnGroup(BaseModel):
    key: str  # merchant or raw description, depending on `by`
    sample_id: str  # a representative transaction id (to drive scope-based apply)
    sample_description: str
    count: int
    total_cents: int


class TxnGroupsOut(BaseModel):
    by: str
    groups: list[TxnGroup]


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


# ------------------------------------------------------------------------- budgets


class BudgetCreate(BaseModel):
    category_id: str
    period: Literal["monthly", "fortnightly", "annual"] = "monthly"
    limit_cents: int = Field(gt=0)


class BudgetUpdate(BaseModel):
    period: Literal["monthly", "fortnightly", "annual"] | None = None
    limit_cents: int | None = Field(default=None, gt=0)


class BudgetOut(BaseModel):
    id: str
    category_id: str
    category_name: str
    parent_name: str | None
    period: str
    period_label: str
    period_start: dt.date
    period_end: dt.date
    limit_cents: int
    actual_cents: int
    remaining_cents: int
    pct_used: float
    projected_cents: int
    status: str  # ok | warning | over


# ----------------------------------------------------------------------- net worth


class NetWorthItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["asset", "liability"]
    value_cents: int = Field(ge=0)


class NetWorthItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    value_cents: int | None = Field(default=None, ge=0)


class NetWorthItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    kind: str
    value_cents: int


class NetWorthPoint(BaseModel):
    as_of: dt.date
    assets_cents: int
    liabilities_cents: int
    net_cents: int


class NetWorthOut(BaseModel):
    assets_cents: int
    liabilities_cents: int
    net_cents: int
    items: list[NetWorthItemOut]
    history: list[NetWorthPoint]


# -------------------------------------------------------------------- savings goals


class SavingsGoalCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    target_cents: int = Field(gt=0)
    target_date: dt.date | None = None
    account_id: str | None = None
    current_cents: int = Field(default=0, ge=0)


class SavingsGoalUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    target_cents: int | None = Field(default=None, gt=0)
    target_date: dt.date | None = None
    account_id: str | None = None
    current_cents: int | None = Field(default=None, ge=0)


class SavingsGoalOut(BaseModel):
    id: str
    name: str
    target_cents: int
    target_date: dt.date | None
    account_id: str | None
    account_name: str | None
    current_cents: int
    remaining_cents: int
    pct_complete: float
    suggested_per_period_cents: int
    period_label: str
    complete: bool


# ------------------------------------------------------------------------ insights


class Insight(BaseModel):
    key: str
    type: str
    severity: str  # alert | warn | info
    title: str
    body: str
    action: str | None = None
    amount_cents: int | None = None
    link: str | None = None


class InsightsOut(BaseModel):
    generated_for: str
    insights: list[Insight]


# ----------------------------------------------------------- recurring & bills


class RecurringSeriesOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    merchant: str
    category_id: str | None
    category_name: str | None
    direction: str  # expense | income
    cadence: str  # weekly | fortnightly | monthly | quarterly | yearly
    interval_days: int
    typical_amount_cents: int
    monthly_amount_cents: int
    occurrences: int
    first_date: dt.date
    last_date: dt.date
    next_due: dt.date
    active: bool
    is_subscription: bool


class RecurringOut(BaseModel):
    series: list[RecurringSeriesOut]
    monthly_committed_cents: int  # active recurring expenses, normalised to a month
    subscriptions_count: int
    subscriptions_monthly_cents: int
    income_monthly_cents: int  # active recurring income, normalised to a month


class UpcomingBillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    due_date: dt.date
    merchant: str
    amount_cents: int
    category_id: str | None
    category_name: str | None
    cadence: str


class UpcomingBillsOut(BaseModel):
    horizon_days: int
    total_cents: int
    bills: list[UpcomingBillOut]


# ----------------------------------------------------------------- forecasting


class ForecastPointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    date: dt.date
    balance_cents: int


class ForecastOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    starting_balance_cents: int
    end_balance_cents: int
    low_balance_cents: int
    low_balance_date: dt.date
    horizon_days: int
    monthly_income_cents: int
    monthly_expense_cents: int
    points: list[ForecastPointOut]


class ForecastAdjustment(BaseModel):
    category_id: str | None = None
    pct: float = Field(ge=-100, le=500)  # -100 = cut entirely, +100 = double


class ForecastRequest(BaseModel):
    days: int = Field(default=90, ge=7, le=365)
    adjustments: list[ForecastAdjustment] = []


# --------------------------------------------------------------- notifications


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    type: str
    severity: str
    title: str
    body: str
    link: str | None
    amount_cents: int | None
    created_at: dt.datetime
    read_at: dt.datetime | None


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    unread: int


class NotificationSettingsOut(BaseModel):
    email_enabled: bool
    digest: str
    large_txn_threshold_cents: int
    low_balance_threshold_cents: int
    smtp_configured: bool  # whether SMTP is set up in the environment


class NotificationSettingsUpdate(BaseModel):
    email_enabled: bool | None = None
    digest: Literal["off", "weekly", "monthly"] | None = None
    large_txn_threshold_cents: int | None = Field(default=None, ge=0)
    low_balance_threshold_cents: int | None = None


class NotificationRunOut(BaseModel):
    households: int
    created: int
    emailed: int
    digests: int


# --------------------------------------------------------------------- benchmarks


class BenchmarkItem(BaseModel):
    category: str
    your_weekly_cents: int
    typical_weekly_cents: int
    diff_cents: int  # your - typical; positive means you spend more
    pct_of_typical: float  # 1.0 = same as typical


class BenchmarkOut(BaseModel):
    basis: str
    adults: int
    children: int
    note: str
    your_total_weekly_cents: int
    typical_total_weekly_cents: int
    items: list[BenchmarkItem]


class MessageOut(BaseModel):
    message: str


class FYReportOption(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    year: int  # financial-year start year
    label: str
    start: dt.date
    end: dt.date


# ----------------------------------------------------------------- AI advisor


class AiSettingsOut(BaseModel):
    provider: str  # none | anthropic | openai
    base_url: str | None
    model: str | None
    privacy_mode: str  # local_only | aggregates | full
    has_key: bool
    configured: bool


class AiSettingsUpdate(BaseModel):
    provider: Literal["none", "anthropic", "openai"] | None = None
    base_url: str | None = None
    model: str | None = None
    privacy_mode: Literal["local_only", "aggregates", "full"] | None = None
    api_key: str | None = None  # write-only; "" clears the stored key


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


class ChatReply(BaseModel):
    reply: str


class AiModelOut(BaseModel):
    id: str
    label: str


# ------------------------------------------------------------------------- updates


class MetaOut(BaseModel):
    version: str


class UpdateStatus(BaseModel):
    current_version: str
    latest_version: str | None
    update_available: bool
    apply_available: bool  # in-app "Update now" works (Watchtower configured)
    check_enabled: bool
    release_url: str | None = None
    release_notes: str | None = None
    published_at: str | None = None
