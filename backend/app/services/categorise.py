"""Categorisation: a high-precision rule engine first (PRD R11), then the
learning ML categoriser (R12). Low-confidence results route to the review queue."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..constants import UNCATEGORISED
from .ml import NaiveBayesCategoriser

ML_THRESHOLD = 0.55


@dataclass
class CategoryResult:
    category_id: str | None
    confidence: float
    source: str  # rule | ml | none


def _match(match_type: str, pattern: str, description: str, merchant: str | None) -> bool:
    p = pattern.lower()
    combined = f"{description} {merchant or ''}".lower()
    if match_type == "equals":
        return description.lower().strip() == p
    if match_type in ("contains", "merchant"):
        return p in combined
    if match_type == "starts_with":
        return combined.lstrip().startswith(p)
    if match_type == "regex":
        try:
            return re.search(pattern, combined, re.IGNORECASE) is not None
        except re.error:
            return False
    return False


class Categoriser:
    def __init__(
        self,
        rules: list[tuple[str, str, str]],
        ml: NaiveBayesCategoriser,
        uncategorised_id: str | None,
    ) -> None:
        self.rules = rules  # (match_type, pattern, category_id), priority-ordered
        self.ml = ml
        self.uncategorised_id = uncategorised_id

    def categorise(self, description: str, merchant: str | None = None) -> CategoryResult:
        for match_type, pattern, category_id in self.rules:
            if _match(match_type, pattern, description, merchant):
                return CategoryResult(category_id, 0.99, "rule")

        predicted, confidence = self.ml.predict(f"{description} {merchant or ''}")
        if predicted and confidence >= ML_THRESHOLD:
            return CategoryResult(predicted, round(confidence, 3), "ml")
        return CategoryResult(None, 0.0, "none")


def build_categoriser(db: Session, household_id: str) -> Categoriser:
    rules = (
        db.execute(
            select(models.CategorisationRule).where(
                models.CategorisationRule.household_id == household_id,
                models.CategorisationRule.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    rule_tuples = [
        (r.match_type, r.pattern, r.category_id) for r in sorted(rules, key=lambda r: r.priority)
    ]

    labelled = db.execute(
        select(
            models.Transaction.raw_description,
            models.Transaction.merchant,
            models.Transaction.category_id,
        ).where(
            models.Transaction.household_id == household_id,
            models.Transaction.category_id.is_not(None),
            models.Transaction.is_transfer.is_(False),
        )
    ).all()
    samples = [(f"{desc} {merchant or ''}", cat) for desc, merchant, cat in labelled if cat]
    ml = NaiveBayesCategoriser()
    ml.train(samples)

    uncategorised_id = db.execute(
        select(models.Category.id).where(
            models.Category.household_id == household_id,
            models.Category.name == UNCATEGORISED,
        )
    ).scalar_one_or_none()

    return Categoriser(rule_tuples, ml, uncategorised_id)


def matches(match_type: str, pattern: str, txn: models.Transaction) -> bool:
    return _match(match_type, pattern, txn.raw_description, txn.merchant)


def apply_rule_to_existing(
    db: Session, household_id: str, match_type: str, pattern: str, category_id: str
) -> int:
    """Backfill a rule over existing rows: fills only blank, unlocked transactions —
    never overrides a category the user already set, and skips exempted rows."""
    candidates = (
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household_id,
                models.Transaction.category_id.is_(None),
                models.Transaction.category_locked.is_(False),
                models.Transaction.is_transfer.is_(False),
                models.Transaction.split_parent_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    updated = 0
    for t in candidates:
        if matches(match_type, pattern, t):
            t.category_id = category_id
            t.confidence = 0.99
            updated += 1
    db.commit()
    return updated


def preview_rule(
    db: Session, household_id: str, match_type: str, pattern: str, sample_limit: int = 6
) -> tuple[int, int, list[str]]:
    """Return (matched, fillable, sample descriptions) for a prospective rule."""
    rows = (
        db.execute(
            select(models.Transaction).where(
                models.Transaction.household_id == household_id,
                models.Transaction.is_transfer.is_(False),
                models.Transaction.split_parent_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    matched = [t for t in rows if matches(match_type, pattern, t)]
    fillable = sum(1 for t in matched if t.category_id is None and not t.category_locked)
    samples = [(t.merchant or t.raw_description) for t in matched[:sample_limit]]
    return len(matched), fillable, samples
