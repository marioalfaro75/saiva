"""Categories and the categorisation rule engine (PRD R11)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer

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


@router.post("/categories", response_model=schemas.CategoryOut, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: schemas.CategoryCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> models.Category:
    if payload.parent_id:
        _check_category(db, payload.parent_id, user.household_id)
    category = models.Category(household_id=user.household_id, **payload.model_dump())
    db.add(category)
    db.commit()
    db.refresh(category)
    return category


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


@router.post("/rules", response_model=schemas.RuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(
    payload: schemas.RuleCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> models.CategorisationRule:
    _check_category(db, payload.category_id, user.household_id)
    rule = models.CategorisationRule(
        household_id=user.household_id, source="user", **payload.model_dump()
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rule(
    rule_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> Response:
    rule = db.get(models.CategorisationRule, rule_id)
    if rule is None or rule.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    db.delete(rule)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
