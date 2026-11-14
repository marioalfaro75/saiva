"""Categories and the categorisation rule engine (PRD R11)."""

from __future__ import annotations

import unicodedata

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import ColumnElement, delete, func, select, update
from sqlalchemy.orm import InstrumentedAttribute, Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services.categorise import (
    RuleDefinition,
    RuleEffect,
    pattern_problem,
    preview_rule,
    reapply_rules,
)

router = APIRouter(tags=["categories"])


def _check_category(db: Session, category_id: str, household_id: str) -> models.Category:
    category = db.get(models.Category, category_id)
    if category is None or category.household_id != household_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown category")
    return category


@router.get("/categories", response_model=list[schemas.CategoryOut])
def list_categories(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[models.Category]:
    return list(
        db.execute(
            select(models.Category)
            .where(models.Category.household_id == user.household_id)
            .order_by(models.Category.sort, models.Category.name)
        )
        .scalars()
        .all()
    )


# Characters that print as nothing. Stripped so a name cannot be invisible, and so
# "Fuel" with a zero-width space after it is not a second, indistinguishable "Fuel".
# The zero-width JOINER is kept: emoji sequences need it.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u2060\ufeff\u00ad"))


def _clean_name(raw: str) -> str:
    # NFC, so "Cafés" typed with a combining accent is the same name as the seeded one;
    # control characters out (a NUL also makes Postgres refuse the value outright).
    text = unicodedata.normalize("NFC", raw).translate(_INVISIBLE)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" or ch.isspace())
    name = " ".join(text.split())
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "A category needs a name")
    return name


def _ensure_unique(db: Session, household_id: str, name: str, *, ignore: str | None = None) -> None:
    """One name, one category, across the household -- case aside.

    Two "Fuel"s would be indistinguishable in every list that offers them, and a search
    that finds one would not say which. The default tree has no repeated names, so this
    costs nothing there.
    """
    # Folded here, in Python, on both sides. Comparing the database's lower() with
    # Python's lower() let exact duplicates through: Postgres lowers "İ" to "i" and a
    # final "Σ" to "σ", Python to "i̇" and "ς". casefold() also equates "Straße" and
    # "STRASSE". A household has at most a few hundred categories.
    wanted = name.casefold()
    names = db.execute(
        select(models.Category.id, models.Category.name).where(
            models.Category.household_id == household_id
        )
    ).all()
    if any(
        cid != ignore and unicodedata.normalize("NFC", other).casefold() == wanted
        for cid, other in names
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, f"There is already a category called {name}")


@router.post("/categories", response_model=schemas.CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: schemas.CategoryCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> models.Category:
    fields = payload.model_dump()
    fields["name"] = _clean_name(payload.name)
    if payload.parent_id:
        parent = _check_category(db, payload.parent_id, user.household_id)
        if parent.parent_id is not None:
            # The tree is two levels deep everywhere it is shown; a third would vanish.
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A category can only go in a group")
        fields["kind"] = parent.kind
    _ensure_unique(db, user.household_id, fields["name"])
    # Last in its group (or last among the groups): the tree is ordered by `sort`.
    highest = db.execute(
        select(func.max(models.Category.sort)).where(
            models.Category.household_id == user.household_id
        )
    ).scalar_one_or_none()
    category = models.Category(household_id=user.household_id, sort=(highest or 0) + 1, **fields)
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


@router.patch("/categories/{category_id}", response_model=schemas.CategoryOut)
def rename_category(
    category_id: str,
    payload: schemas.CategoryRename,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> models.Category:
    """Rename only. Safe for built-in categories too: what the app looks them up by is
    their system_key, which a rename leaves alone."""
    category = _owned(db, category_id, user.household_id)
    name = _clean_name(payload.name)
    _ensure_unique(db, user.household_id, name, ignore=category.id)
    category.name = name
    db.commit()
    db.refresh(category)
    return category


@router.get("/categories/{category_id}/usage", response_model=schemas.CategoryUsage)
def category_usage(
    category_id: str,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.CategoryUsage:
    category = _owned(db, category_id, user.household_id)
    return _usage(db, category)


@router.delete("/categories/{category_id}", response_model=schemas.CategoryRemoved)
def remove_category(
    category_id: str,
    move_to: str | None = None,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.CategoryRemoved:
    """Remove a category, moving what pointed at it -- in one transaction.

    Its transactions go to `move_to`, or become uncategorised when it is omitted. Its
    rules follow them; with nowhere to go they are removed, because a rule that files
    things as "uncategorised" does nothing. Its budget is removed: the destination may
    have a budget of its own, and two budgets for one category cannot exist.
    """
    category = _owned(db, category_id, user.household_id)
    if category.protected:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{category.name} cannot be removed: the app files transactions into it. "
            "You can rename it.",
        )
    usage = _usage(db, category)
    if usage.subcategories:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{category.name} still has categories in it. Remove those first.",
        )
    if move_to is not None:
        if move_to == category.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a different category")
        _check_category(db, move_to, user.household_id)

    # The counts are the ones just measured, in this same transaction, of exactly the
    # rows the statements below change.
    db.execute(
        update(models.Transaction)
        .where(
            models.Transaction.household_id == user.household_id,
            models.Transaction.category_id == category.id,
        )
        .values(category_id=move_to)
    )
    rules = (
        models.CategorisationRule.household_id == user.household_id,
        models.CategorisationRule.category_id == category.id,
    )
    if move_to is not None:
        db.execute(update(models.CategorisationRule).where(*rules).values(category_id=move_to))
    else:
        db.execute(delete(models.CategorisationRule).where(*rules))
    db.execute(
        delete(models.Budget).where(
            models.Budget.household_id == user.household_id,
            models.Budget.category_id == category.id,
        )
    )
    db.delete(category)
    db.commit()
    return schemas.CategoryRemoved(
        moved_transactions=usage.transactions,
        moved_rules=usage.rules if move_to is not None else 0,
        removed_rules=0 if move_to is not None else usage.rules,
        removed_budgets=usage.budgets,
    )


def _owned(db: Session, category_id: str, household_id: str) -> models.Category:
    category = db.get(models.Category, category_id)
    if category is None or category.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    return category


def _usage(db: Session, category: models.Category) -> schemas.CategoryUsage:
    household = category.household_id

    # Takes the column and the filters, not a built select(), so the helper's
    # signature does not depend on how a SQLAlchemy release types select().
    def count(column: InstrumentedAttribute[str], *conditions: ColumnElement[bool]) -> int:
        return int(db.execute(select(func.count(column)).where(*conditions)).scalar_one())

    return schemas.CategoryUsage(
        transactions=count(
            models.Transaction.id,
            models.Transaction.household_id == household,
            models.Transaction.category_id == category.id,
        ),
        rules=count(
            models.CategorisationRule.id,
            models.CategorisationRule.household_id == household,
            models.CategorisationRule.category_id == category.id,
        ),
        budgets=count(
            models.Budget.id,
            models.Budget.household_id == household,
            models.Budget.category_id == category.id,
        ),
        subcategories=count(
            models.Category.id,
            models.Category.household_id == household,
            models.Category.parent_id == category.id,
        ),
    )


@router.get("/rules", response_model=list[schemas.RuleOut])
def list_rules(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[models.CategorisationRule]:
    return list(
        db.execute(
            select(models.CategorisationRule)
            .where(models.CategorisationRule.household_id == user.household_id)
            .order_by(models.CategorisationRule.priority)
        )
        .scalars()
        .all()
    )


@router.post("/rules", response_model=schemas.RuleChangeOut, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: schemas.RuleCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.RuleChangeOut:
    _check_category(db, payload.category_id, user.household_id)
    _check_pattern(payload.match_type, payload.pattern)
    rule = models.CategorisationRule(
        household_id=user.household_id, source="user", **payload.model_dump()
    )
    db.add(rule)
    db.flush()
    # A rule takes effect when it is made, on the transactions already there -- not
    # only on the next import. The preview said what it would do; this does it.
    effect = reapply_rules(db, user.household_id, None, RuleDefinition.of(rule))
    db.commit()
    db.refresh(rule)
    return _change_out(rule, effect)


def _change_out(rule: models.CategorisationRule, effect: RuleEffect) -> schemas.RuleChangeOut:
    return schemas.RuleChangeOut(
        **schemas.RuleOut.model_validate(rule).model_dump(),
        categorised=effect.categorised,
        uncategorised=effect.uncategorised,
    )


@router.post("/rules/preview", response_model=schemas.RulePreviewOut)
def preview_rule_endpoint(
    payload: schemas.RulePreviewRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.RulePreviewOut:
    if payload.category_id is not None:
        _check_category(db, payload.category_id, user.household_id)
    _check_pattern(payload.match_type, payload.pattern)
    result = preview_rule(
        db,
        user.household_id,
        payload.match_type,
        payload.pattern,
        payload.category_id,
        priority=schemas.RuleCreate.default_priority(),
    )
    names = dict(
        db.execute(
            select(models.Category.id, models.Category.name).where(
                models.Category.household_id == user.household_id
            )
        )
        .tuples()
        .all()
    )
    rows = [
        schemas.RulePreviewRow(
            id=r.txn.id,
            txn_date=r.txn.txn_date,
            description=r.txn.merchant or r.txn.raw_description,
            amount_cents=r.txn.amount_cents,
            category_id=r.txn.category_id,
            category_name=names.get(r.txn.category_id) if r.txn.category_id else None,
            outcome=r.outcome,
            other_rule_pattern=r.other_rule.pattern if r.other_rule else None,
            other_rule_category_name=names.get(r.other_rule.category_id) if r.other_rule else None,
        )
        for r in result.rows
    ]
    return schemas.RulePreviewOut(
        matched=result.matched,
        fillable=result.fillable,
        samples=result.samples,
        will_change=result.outcomes["change"],
        already=result.outcomes["same"],
        locked=result.outcomes["locked"],
        kept=result.outcomes["kept"],
        other_rule=result.outcomes["other_rule"],
        rows=rows,
    )


def _check_pattern(match_type: str, pattern: str) -> None:
    problem = pattern_problem(match_type, pattern)
    if problem is not None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, problem)


def _owned_rule(db: Session, rule_id: str, household_id: str) -> models.CategorisationRule:
    rule = db.get(models.CategorisationRule, rule_id)
    if rule is None or rule.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    return rule


@router.patch("/rules/{rule_id}", response_model=schemas.RuleChangeOut)
def update_rule(
    rule_id: str,
    payload: schemas.RuleUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.RuleChangeOut:
    rule = _owned_rule(db, rule_id, user.household_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("category_id") is not None:
        _check_category(db, data["category_id"], user.household_id)
    _check_pattern(
        data.get("match_type") or rule.match_type, data.get("pattern") or rule.pattern
    )
    before = RuleDefinition.of(rule)
    for key, value in data.items():
        # An explicit null is "leave it": these columns are not nullable.
        if value is not None:
            setattr(rule, key, value)
    db.flush()
    # Editing, switching off and switching on are all a change in what the rule files:
    # what it no longer catches goes back, and what it newly catches is filed.
    effect = reapply_rules(db, user.household_id, before, RuleDefinition.of(rule))
    db.commit()
    db.refresh(rule)
    return _change_out(rule, effect)


@router.post("/rules/{rule_id}/apply", response_model=schemas.CountOut)
def apply_rule(
    rule_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.CountOut:
    rule = _owned_rule(db, rule_id, user.household_id)
    effect = reapply_rules(db, user.household_id, None, RuleDefinition.of(rule))
    db.commit()
    return schemas.CountOut(updated=effect.categorised)


@router.delete("/rules/{rule_id}", response_model=schemas.RuleEffectOut)
def delete_rule(
    rule_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.RuleEffectOut:
    rule = _owned_rule(db, rule_id, user.household_id)
    before = RuleDefinition.of(rule)
    db.delete(rule)
    db.flush()
    # What the rule filed goes back to Uncategorised, unless another rule catches it.
    effect = reapply_rules(db, user.household_id, before, None)
    db.commit()
    return schemas.RuleEffectOut(categorised=effect.categorised, uncategorised=effect.uncategorised)
