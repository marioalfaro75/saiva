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


def _match(match_type: str, pattern: str, text: str) -> bool:
    p = pattern.lower()
    if match_type in ("contains", "merchant"):
        return p in text
    if match_type == "starts_with":
        return text.lstrip().startswith(p)
    if match_type == "regex":
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
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
        text = f"{description} {merchant or ''}".lower()
        for match_type, pattern, category_id in self.rules:
            if _match(match_type, pattern, text):
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
