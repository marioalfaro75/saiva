"""Budget CRUD with live progress (PRD R24)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import budgets as budgets_service

router = APIRouter(prefix="/budgets", tags=["budgets"])


def _household(db: Session, user: models.User) -> models.Household:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return household


def _get_owned(db: Session, budget_id: str, household_id: str) -> models.Budget:
    budget = db.get(models.Budget, budget_id)
    if budget is None or budget.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Budget not found")
    return budget


@router.get("", response_model=list[schemas.BudgetOut])
def list_budgets(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[schemas.BudgetOut]:
    return budgets_service.list_budgets(db, _household(db, user))


@router.post("", response_model=schemas.BudgetOut, status_code=status.HTTP_201_CREATED)
def create_budget(
    payload: schemas.BudgetCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.BudgetOut:
    category = db.get(models.Category, payload.category_id)
    if category is None or category.household_id != user.household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    existing = db.execute(
        select(models.Budget.id).where(
            models.Budget.household_id == user.household_id,
            models.Budget.category_id == payload.category_id,
        )
    ).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "A budget for this category already exists.")
    budget = models.Budget(household_id=user.household_id, **payload.model_dump())
    db.add(budget)
    db.commit()
    db.refresh(budget)
    return budgets_service.compute_budget(db, _household(db, user), budget)


@router.patch("/{budget_id}", response_model=schemas.BudgetOut)
def update_budget(
    budget_id: str,
    payload: schemas.BudgetUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.BudgetOut:
    budget = _get_owned(db, budget_id, user.household_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(budget, key, value)
    db.commit()
    db.refresh(budget)
    return budgets_service.compute_budget(db, _household(db, user), budget)


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_budget(
    budget_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> Response:
    budget = _get_owned(db, budget_id, user.household_id)
    db.delete(budget)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
