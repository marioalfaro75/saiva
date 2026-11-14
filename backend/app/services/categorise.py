"""Categorisation: a high-precision rule engine first (PRD R11), then the
learning ML categoriser (R12). Low-confidence results route to the review queue."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import regex
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..constants import UNCATEGORISED
from .ml import NaiveBayesCategoriser

ML_THRESHOLD = 0.55

# What a transaction's confidence says about who set its category. A rule writes exactly
# RULE_CONFIDENCE and a person exactly 1.0 (the recategorise and bulk endpoints), so the
# learner's own score is kept below both: without that cap a confident guess would be
# indistinguishable from a rule's work or a person's choice, and a rule change would undo
# a guess -- or never touch one -- for no visible reason.
RULE_CONFIDENCE = 0.99
ML_MAX_CONFIDENCE = 0.98
PERSON_CONFIDENCE = 1.0

# Where a rule made from the Rules form or the categorise dialog sits: ahead of the
# built-in starter rules (50), so a household's own rule wins where the two overlap.
USER_RULE_PRIORITY = 10


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
        return _regex_matches(pattern, combined)
    return False


# A rule's pattern is written by a household member and run against every transaction,
# so it is untrusted input on a hot path. The stdlib engine backtracks catastrophically:
# "^(a+)+$" against 31 characters takes ~55 seconds, and nothing bounds how long a match
# may run, so any signed-in member — a viewer included — could pin a core indefinitely.
# The `regex` engine defeats that class outright, and the timeout catches whatever it
# does not.
_REGEX_TIMEOUT_SECONDS = 0.1


def _regex_matches(pattern: str, subject: str) -> bool:
    try:
        return (
            regex.search(
                pattern, subject, regex.IGNORECASE, timeout=_REGEX_TIMEOUT_SECONDS
            )
            is not None
        )
    except regex.error:
        return False
    except TimeoutError:
        # Treated as "no match" rather than an error: one pathological rule should not
        # fail an entire import, and the rule simply stops being useful.
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
                return CategoryResult(category_id, RULE_CONFIDENCE, "rule")

        predicted, confidence = self.ml.predict(f"{description} {merchant or ''}")
        if predicted and confidence >= ML_THRESHOLD:
            return CategoryResult(predicted, round(min(confidence, ML_MAX_CONFIDENCE), 3), "ml")
        return CategoryResult(None, 0.0, "none")


def ordered_rules(db: Session, household_id: str) -> list[models.CategorisationRule]:
    """A household's active rules in the order they are tried; the first match wins.

    Priority first, then age, then id. Two rules at the same priority used to be tried in
    whatever order the database returned them, so which of two overlapping rules won
    could differ between an import and a later re-run. Oldest first: a new rule fills in
    around the rules already there instead of quietly taking transactions from them.
    """
    return list(
        db.execute(
            select(models.CategorisationRule)
            .where(
                models.CategorisationRule.household_id == household_id,
                models.CategorisationRule.is_active.is_(True),
            )
            .order_by(
                models.CategorisationRule.priority,
                models.CategorisationRule.created_at,
                models.CategorisationRule.id,
            )
        )
        .scalars()
        .all()
    )


def build_categoriser(db: Session, household_id: str) -> Categoriser:
    rule_tuples = [
        (r.match_type, r.pattern, r.category_id) for r in ordered_rules(db, household_id)
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
            # By key, not name: the household may rename "Uncategorised".
            models.Category.system_key == UNCATEGORISED,
        )
    ).scalar_one_or_none()

    return Categoriser(rule_tuples, ml, uncategorised_id)


def matches(match_type: str, pattern: str, txn: models.Transaction) -> bool:
    return _match(match_type, pattern, txn.raw_description, txn.merchant)


def set_by_person(t: models.Transaction) -> bool:
    """A category someone chose: through the app (1.0), or with no score at all -- a
    transaction added by hand with its category, or data that predates scores. Rules
    never replace one; treating an unknown origin as a person's choice errs that way."""
    return t.category_id is not None and (t.confidence is None or t.confidence >= PERSON_CONFIDENCE)


def set_by_rule(t: models.Transaction) -> bool:
    return t.category_id is not None and t.confidence == RULE_CONFIDENCE


def rules_may_change(t: models.Transaction) -> bool:
    """Rules decide blank and automatic categories only: never a locked row, a person's
    choice, a transfer (filed by transfer detection), or one part of a split."""
    return (
        not t.category_locked
        and not t.is_transfer
        and t.split_parent_id is None
        and not set_by_person(t)
    )


def _rule_scope(db: Session, household_id: str) -> list[models.Transaction]:
    """Every transaction a rule can be asked about: transfers and split parts excluded."""
    return list(
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


@dataclass(frozen=True)
class RuleDefinition:
    """What a rule matches and where it files, captured before an edit changes it."""

    rule_id: str
    match_type: str
    pattern: str
    category_id: str
    is_active: bool = True
    created_at: dt.datetime | None = None

    @classmethod
    def of(cls, rule: models.CategorisationRule) -> RuleDefinition:
        return cls(
            rule.id,
            rule.match_type,
            rule.pattern,
            rule.category_id,
            rule.is_active,
            rule.created_at,
        )


# created_at and updated_at are two clock reads at insert, microseconds apart: a row
# whose two are closer than this has not been changed since it was created.
UNTOUCHED = dt.timedelta(seconds=2)


def could_have_filed(rule: RuleDefinition, t: models.Transaction) -> bool:
    """Whether `rule` can be what put `t` where it is.

    A rule files a row either when the row arrives (the rule is older than it) or by
    changing it later (the row was last changed after the rule was made). A rule
    younger than a row that has not changed since the rule was made did neither: the
    row's rule mark is another rule's, or a guess an older release stored as exactly
    0.99 -- and taking this rule away must not take that row's category with it.
    """
    if rule.created_at is None:
        return True
    return rule.created_at <= t.created_at or t.updated_at >= rule.created_at


def pattern_problem(match_type: str, pattern: str) -> str | None:
    """Why a pattern can never match, or None. Only a regular expression can be
    malformed; one that is would be saved and then silently match nothing, forever."""
    if match_type != "regex":
        return None
    try:
        regex.compile(pattern, regex.IGNORECASE)
    except regex.error as exc:
        return f"That isn't a valid regular expression ({exc})."
    return None


@dataclass
class RuleEffect:
    categorised: int = 0  # moved into a rule's category
    uncategorised: int = 0  # a rule's category taken away, with no other rule to take over


def _first_match(
    rules: Sequence[models.CategorisationRule], t: models.Transaction
) -> models.CategorisationRule | None:
    return next((r for r in rules if matches(r.match_type, r.pattern, t)), None)


def reapply_rules(
    db: Session,
    household_id: str,
    before: RuleDefinition | None,
    after: RuleDefinition | None,
) -> RuleEffect:
    """Bring transactions in line with one rule's change: made, edited, switched off or
    on, or deleted. `before` is the rule as it was (None for a new rule), `after` as it
    is now (None once deleted); the change itself must already be flushed.

    It moves only what this rule decides, so the change does what its preview said:

    * a transaction the rule now matches, and wins -- no earlier rule matches it -- is
      filed in the rule's category. One an earlier rule wins is left where it is: that
      rule's business, not this change's.
    * a transaction the rule had filed -- matched by it before, in its category, with a
      rule's mark -- and no longer wins goes to whichever rule wins it now, or back to
      Uncategorised if none does.

    Nothing a person chose or locked is touched, and neither is a transfer or part of a
    split. A guess the rule never took over stays a guess.
    """
    rules = ordered_rules(db, household_id)
    changed = after.rule_id if after is not None and after.is_active else None
    effect = RuleEffect()
    for t in _rule_scope(db, household_id):
        matched_now = (
            after is not None and after.is_active and matches(after.match_type, after.pattern, t)
        )
        filed_by_it = (
            before is not None
            and before.is_active
            and set_by_rule(t)
            and t.category_id == before.category_id
            and could_have_filed(before, t)
            and matches(before.match_type, before.pattern, t)
        )
        if not (matched_now or filed_by_it) or not rules_may_change(t):
            continue
        winner = _first_match(rules, t)
        if not filed_by_it and (winner is None or winner.id != changed):
            continue
        if winner is not None:
            if t.category_id != winner.category_id:
                effect.categorised += 1
            t.category_id = winner.category_id
            t.confidence = RULE_CONFIDENCE
        else:
            t.category_id = None
            t.confidence = None
            effect.uncategorised += 1
    return effect


# What a prospective rule would do to one transaction it matches.
OUTCOMES = ("change", "same", "locked", "kept", "other_rule")


@dataclass
class PreviewRow:
    txn: models.Transaction
    outcome: str
    other_rule: models.CategorisationRule | None = None


@dataclass
class RulePreview:
    matched: int
    fillable: int
    samples: list[str]
    outcomes: Counter[str]
    rows: list[PreviewRow]


def preview_rule(
    db: Session,
    household_id: str,
    match_type: str,
    pattern: str,
    category_id: str | None = None,
    row_limit: int = 100,
    priority: int = USER_RULE_PRIORITY,
) -> RulePreview:
    """What a rule not yet made would do: every transaction it matches, newest first,
    each with its outcome -- the same decision reapply_rules makes when it is saved.

    A new rule is tried after every existing rule of the same or a lower priority
    number (oldest first among equals). Where one of those matches too, it wins and the
    transaction stays where it is ("other_rule"). Without a category chosen yet, a
    transaction the rule would set is a "change".
    """
    ahead = [r for r in ordered_rules(db, household_id) if r.priority <= priority]
    matched = sorted(
        (t for t in _rule_scope(db, household_id) if matches(match_type, pattern, t)),
        key=lambda t: (t.txn_date, t.id),
        reverse=True,
    )
    rows: list[PreviewRow] = []
    for t in matched:
        if category_id is not None and t.category_id == category_id:
            rows.append(PreviewRow(t, "same"))
        elif t.category_locked:
            rows.append(PreviewRow(t, "locked"))
        elif set_by_person(t):
            rows.append(PreviewRow(t, "kept"))
        elif (first := _first_match(ahead, t)) is not None:
            rows.append(PreviewRow(t, "other_rule", first))
        else:
            rows.append(PreviewRow(t, "change"))

    samples: list[str] = []
    for t in matched:
        name = t.merchant or t.raw_description
        if name not in samples:
            samples.append(name)
        if len(samples) == 6:
            break
    return RulePreview(
        matched=len(matched),
        fillable=sum(1 for t in matched if t.category_id is None and not t.category_locked),
        samples=samples,
        outcomes=Counter(r.outcome for r in rows),
        rows=rows[:row_limit],
    )
