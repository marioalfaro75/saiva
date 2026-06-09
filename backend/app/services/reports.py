"""Financial-year report (PRD R32): a tidy PDF for the accountant — totals, spend
by category, a month-by-month breakdown and top merchants for one FY. Built from
the same spendable-leaf logic as the dashboard, so the numbers match the app."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from dateutil.relativedelta import relativedelta
from fpdf import FPDF
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models
from ..schemas import CategoryBreakdownItem
from .dashboard import _spendable_leaves, category_breakdown, summary
from .periods import fy_bounds


@dataclass
class FYOption:
    year: int  # FY start year
    label: str
    start: dt.date
    end: dt.date


@dataclass
class FYReport:
    label: str
    start: dt.date
    end: dt.date
    income_cents: int
    expense_cents: int
    net_cents: int
    savings_rate: float
    txn_count: int
    categories: list[CategoryBreakdownItem]
    monthly: list[tuple[str, int, int]]  # month label, income, expense
    top_merchants: list[tuple[str, int]]


def _fy_option(household: models.Household, fy_start_year: int) -> FYOption:
    start = dt.date(fy_start_year, household.fy_start_month, household.fy_start_day)
    end = start + relativedelta(years=1) - dt.timedelta(days=1)
    return FYOption(year=fy_start_year, label=f"FY{end.year}", start=start, end=end)


def available_years(
    db: Session, household: models.Household, today: dt.date | None = None
) -> list[FYOption]:
    today = today or dt.date.today()
    min_d, max_d = db.execute(
        select(func.min(models.Transaction.txn_date), func.max(models.Transaction.txn_date)).where(
            models.Transaction.household_id == household.id
        )
    ).one()
    current_start, _ = fy_bounds(household, today)
    if min_d is None:
        years = [current_start.year]
    else:
        first_start, _ = fy_bounds(household, min_d)
        last_start, _ = fy_bounds(household, max_d)
        years = list(range(first_start.year, max(last_start.year, current_start.year) + 1))
    return [_fy_option(household, y) for y in reversed(years)]


def build_fy_report(
    db: Session, household: models.Household, fy_start_year: int
) -> FYReport:
    opt = _fy_option(household, fy_start_year)
    s = summary(db, household, "custom", opt.start, opt.end)
    cb = category_breakdown(db, household, "custom", opt.start, opt.end)
    leaves = _spendable_leaves(db, household.id, opt.start, opt.end)

    monthly: list[tuple[str, int, int]] = []
    month = opt.start.replace(day=1)
    while month <= opt.end:
        m_end = month + relativedelta(months=1) - dt.timedelta(days=1)
        seg = [t for t in leaves if month <= t.txn_date <= m_end]
        income = sum(t.amount_cents for t in seg if t.amount_cents > 0)
        expense = -sum(t.amount_cents for t in seg if t.amount_cents < 0)
        monthly.append((month.strftime("%b %Y"), income, expense))
        month += relativedelta(months=1)

    merchants: dict[str, int] = {}
    for t in leaves:
        if t.amount_cents < 0 and t.merchant:
            merchants[t.merchant] = merchants.get(t.merchant, 0) + (-t.amount_cents)
    top = sorted(merchants.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return FYReport(
        label=opt.label,
        start=opt.start,
        end=opt.end,
        income_cents=s.income_cents,
        expense_cents=s.expense_cents,
        net_cents=s.net_cents,
        savings_rate=s.savings_rate,
        txn_count=s.txn_count,
        categories=cb.items,
        monthly=monthly,
        top_merchants=top,
    )


def _m(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _safe(text: str) -> str:
    """fpdf2's built-in fonts are latin-1 only; fold anything else to a close
    ASCII-ish equivalent so user-supplied names never break rendering."""
    return text.encode("latin-1", "replace").decode("latin-1")


def _section(pdf: FPDF, title: str) -> None:
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(0)
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)


def _kv(pdf: FPDF, label: str, value: str) -> None:
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(70, 7, label)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")


def _three(pdf: FPDF, a: str, b: str, c: str, *, header: bool = False) -> None:
    pdf.set_font("Helvetica", "B" if header else "", 10)
    if header:
        pdf.set_text_color(120)
    else:
        pdf.set_text_color(0)
    pdf.cell(95, 6, _safe(a))
    pdf.cell(40, 6, b, align="R")
    pdf.cell(0, 6, c, align="R", new_x="LMARGIN", new_y="NEXT")


def render_pdf(household: models.Household, r: FYReport) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, _safe(f"{household.name} - {r.label}"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(120)
    pdf.cell(
        0, 6, f"{r.start:%d %b %Y} to {r.end:%d %b %Y}", new_x="LMARGIN", new_y="NEXT"
    )
    pdf.set_text_color(0)

    _section(pdf, "Summary")
    _kv(pdf, "Income", _m(r.income_cents))
    _kv(pdf, "Expenses", _m(r.expense_cents))
    _kv(pdf, "Net", _m(r.net_cents))
    _kv(pdf, "Savings rate", f"{r.savings_rate * 100:.1f}%")
    _kv(pdf, "Transactions", str(r.txn_count))

    _section(pdf, "Spending by category")
    _three(pdf, "Category", "Amount", "% of spend", header=True)
    for item in r.categories[:20]:
        name = item.category_name
        if item.parent_name:
            name = f"{item.parent_name} / {name}"
        _three(pdf, name[:60], _m(item.amount_cents), f"{item.pct * 100:.1f}%")

    _section(pdf, "Month by month")
    _three(pdf, "Month", "Income", "Expenses", header=True)
    for label, income, expense in r.monthly:
        _three(pdf, label, _m(income), _m(expense))

    if r.top_merchants:
        _section(pdf, "Top merchants")
        _three(pdf, "Merchant", "Amount", "", header=True)
        for merchant, amount in r.top_merchants:
            _three(pdf, merchant[:60], _m(amount), "")

    return bytes(pdf.output())
