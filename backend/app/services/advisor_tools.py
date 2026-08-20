"""Read-only lookups the advisor can call while answering (PRD §10).

A fixed snapshot can only ever answer the questions it was built to answer. These
let the model fetch what a specific question actually needs — "is there a
transaction mentioning Helen", "who do I spend the most with" — instead of the
snapshot having to guess in advance.

Everything here is read-only and scoped to one household. Which tools a model is
offered depends on the privacy mode: the ones that return transaction detail are
simply not present in aggregates mode, so the model cannot ask for what the
household chose not to share.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import models
from ..constants import UNCATEGORISED
from .dashboard import _spendable_leaves, category_breakdown, summary
from .periods import ResolvedPeriod, resolve

# Caps so one call cannot flood the context window.
MAX_ROWS = 40
MAX_GROUPS = 25
# How many times the model may call tools before it has to answer. Enough for a
# lookup plus a follow-up, low enough that a confused model cannot loop.
MAX_TOOL_ROUNDS = 4

# Tools that reveal individual transactions. Withheld unless the privacy mode
# permits raw detail, so aggregates mode cannot be talked around.
DETAIL_TOOLS = {"search_transactions", "list_transactions"}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]


TOOLS: list[Tool] = [
    Tool(
        name="search_transactions",
        description=(
            "Find transactions whose description, merchant, category or account "
            "matches some text. Use for questions like 'do I have any transactions "
            "mentioning X'. Searches the household's whole history unless a period "
            "is given."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to look for."},
                "period": {
                    "type": "string",
                    "description": (
                        "Optional period, e.g. 'fy:2024', 'month:2025-03' or 'all'. "
                        "Defaults to the period the user is viewing."
                    ),
                },
            },
            "required": ["text"],
        },
    ),
    Tool(
        name="list_transactions",
        description=(
            "List transactions for a period, largest first, optionally only "
            "uncategorised ones. Use to inspect what makes up a total."
        ),
        parameters={
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "Optional period token."},
                "uncategorised_only": {"type": "boolean"},
            },
        },
    ),
    Tool(
        name="spending_by_category",
        description="Total spending per category for a period, biggest first.",
        parameters={
            "type": "object",
            "properties": {"period": {"type": "string"}},
        },
    ),
    Tool(
        name="spending_by_merchant",
        description=(
            "Total spending per merchant for a period, biggest first. Answers 'who "
            "do I spend the most with'."
        ),
        parameters={
            "type": "object",
            "properties": {"period": {"type": "string"}},
        },
    ),
    Tool(
        name="compare_periods",
        description=(
            "Income, expenses and net for two periods side by side. Use for 'how "
            "does this year compare with last'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "period_a": {"type": "string", "description": "e.g. 'fy:2025'"},
                "period_b": {"type": "string", "description": "e.g. 'fy:2024'"},
            },
            "required": ["period_a", "period_b"],
        },
    ),
]


def tools_for(privacy_mode: str) -> list[Tool]:
    """The tools a model may use. Detail lookups are absent in aggregates mode, so
    the model can see it has no way to reach transaction text and says so."""
    if privacy_mode in ("full", "local_only"):
        return TOOLS
    return [t for t in TOOLS if t.name not in DETAIL_TOOLS]


def _money(cents: int) -> str:
    """Formatted the way the app shows it: -$1,200.00, not $-1,200.00."""
    return f"-${abs(cents) / 100:,.2f}" if cents < 0 else f"${cents / 100:,.2f}"


KNOWN_PERIODS = {
    "this_month", "last_month", "last_30d", "last_90d", "this_period", "this_fy", "all",
}
KNOWN_PERIOD_PREFIXES = {"fy", "q", "month"}


def _window(
    household: models.Household, token: str | None, default: ResolvedPeriod
) -> ResolvedPeriod:
    """Resolve a period the model asked for, falling back to the one the user is
    viewing. Anything unrecognised has to fall back rather than resolve, or it would
    quietly become "this financial year" and answer about the wrong window."""
    if not token:
        return default
    if token not in KNOWN_PERIODS and token.split(":")[0] not in KNOWN_PERIOD_PREFIXES:
        return default
    try:
        return resolve(household, token, all_bounds=(dt.date(1900, 1, 1), dt.date(2100, 1, 1)))
    except ValueError:
        return default


def _describe(t: models.Transaction) -> str:
    return f"{t.txn_date:%d %b %Y} {t.raw_description or t.merchant or '(no description)'}"


def run(
    db: Session,
    household: models.Household,
    privacy_mode: str,
    name: str,
    args: dict[str, Any],
    default_window: ResolvedPeriod,
) -> str:
    """Execute one tool call and return a plain-text result for the model.

    Re-checks the privacy mode rather than trusting that the tool was withheld: a
    model can invent a call for a tool it was never given.
    """
    if name in DETAIL_TOOLS and privacy_mode not in ("full", "local_only"):
        return (
            "Not available: the household's AI privacy mode is 'Aggregates only', so "
            "individual transactions cannot be shared. Tell them they can change it "
            "in Settings > AI advisor."
        )

    if name == "compare_periods":
        a = _window(household, str(args.get("period_a") or ""), default_window)
        b = _window(household, str(args.get("period_b") or ""), default_window)
        out = []
        for w in (a, b):
            s = summary(db, household, "custom", w.start, w.end)
            out.append(
                f"{w.label} ({w.start:%d %b %Y}–{w.end:%d %b %Y}): income "
                f"{_money(s.income_cents)}, expenses {_money(s.expense_cents)}, "
                f"net {_money(s.net_cents)}"
            )
        return "\n".join(out)

    window = _window(household, args.get("period"), default_window)

    if name == "spending_by_category":
        cb = category_breakdown(db, household, "custom", window.start, window.end)
        if not cb.items:
            return f"No spending recorded in {window.label}."
        rows = [
            f"- {it.category_name}: {_money(it.amount_cents)} ({it.pct * 100:.0f}%)"
            for it in cb.items[:MAX_GROUPS]
        ]
        return f"Spending by category in {window.label}:\n" + "\n".join(rows)

    txns = _spendable_leaves(db, household.id, window.start, window.end)

    if name == "spending_by_merchant":
        totals: dict[str, list[int]] = {}
        for t in txns:
            if t.amount_cents >= 0:
                continue
            key = t.merchant or t.raw_description or "(no description)"
            entry = totals.setdefault(key, [0, 0])
            entry[0] += -t.amount_cents
            entry[1] += 1
        if not totals:
            return f"No spending recorded in {window.label}."
        ranked = sorted(totals.items(), key=lambda kv: -kv[1][0])[:MAX_GROUPS]
        rows = [f"- {n}: {_money(v[0])} across {v[1]} transaction(s)" for n, v in ranked]
        return f"Spending by merchant in {window.label}:\n" + "\n".join(rows)

    if name == "list_transactions":
        names = {
            c.id: c.name
            for c in db.execute(
                select(models.Category).where(models.Category.household_id == household.id)
            )
            .scalars()
            .all()
        }
        rows_in = [t for t in txns if t.amount_cents < 0]
        if args.get("uncategorised_only"):
            rows_in = [
                t
                for t in rows_in
                if t.category_id is None or names.get(t.category_id) == UNCATEGORISED
            ]
        if not rows_in:
            return f"No matching transactions in {window.label}."
        biggest = sorted(rows_in, key=lambda t: t.amount_cents)[:MAX_ROWS]
        rows = [f"- {_describe(t)}: {_money(t.amount_cents)}" for t in biggest]
        more = len(rows_in) - len(biggest)
        text = f"{len(rows_in)} transaction(s) in {window.label}:\n" + "\n".join(rows)
        return text + (f"\n…and {more} more." if more > 0 else "")

    if name == "search_transactions":
        text = str(args.get("text") or "").strip()
        if not text:
            return "No search text was given."
        like = f"%{text}%"
        found = (
            db.execute(
                select(models.Transaction)
                .outerjoin(models.Account)
                .outerjoin(models.Category)
                .where(
                    models.Transaction.household_id == household.id,
                    or_(
                        models.Transaction.raw_description.ilike(like),
                        models.Transaction.merchant.ilike(like),
                        models.Transaction.notes.ilike(like),
                        models.Account.name.ilike(like),
                        models.Category.name.ilike(like),
                    ),
                )
                .order_by(models.Transaction.txn_date.desc())
                .limit(MAX_ROWS + 1)
            )
            .scalars()
            .all()
        )
        if not found:
            return f"No transactions match '{text}' anywhere in the household's history."
        shown = found[:MAX_ROWS]
        rows = [f"- {_describe(t)}: {_money(t.amount_cents)}" for t in shown]
        text_out = f"{len(shown)} transaction(s) matching '{text}':\n" + "\n".join(rows)
        return text_out + ("\n…and more beyond this limit." if len(found) > MAX_ROWS else "")

    return f"Unknown tool '{name}'."
