"""Savings goals CRUD with computed progress and suggested contribution (R25)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import goals as goals_service

router = APIRouter(prefix="/goals", tags=["goals"])


def _household(db: Session, user: models.User) -> models.Household:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return household


def _get_owned(db: Session, goal_id: str, household_id: str) -> models.SavingsGoal:
    goal = db.get(models.SavingsGoal, goal_id)
    if goal is None or goal.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Goal not found")
    return goal


def _validate_account(db: Session, account_id: str | None, household_id: str) -> None:
    if account_id is None:
        return
    account = db.get(models.Account, account_id)
    if account is None or account.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Linked account not found")


@router.get("", response_model=list[schemas.SavingsGoalOut])
def list_goals(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[schemas.SavingsGoalOut]:
    return goals_service.list_goals(db, _household(db, user))


@router.post("", response_model=schemas.SavingsGoalOut, status_code=status.HTTP_201_CREATED)
def create_goal(
    payload: schemas.SavingsGoalCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.SavingsGoalOut:
    _validate_account(db, payload.account_id, user.household_id)
    goal = models.SavingsGoal(household_id=user.household_id, **payload.model_dump())
    db.add(goal)
    db.commit()
    db.refresh(goal)
    return goals_service.compute_goal(db, _household(db, user), goal)


@router.patch("/{goal_id}", response_model=schemas.SavingsGoalOut)
def update_goal(
    goal_id: str,
    payload: schemas.SavingsGoalUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.SavingsGoalOut:
    goal = _get_owned(db, goal_id, user.household_id)
    data = payload.model_dump(exclude_unset=True)
    if "account_id" in data:
        _validate_account(db, data["account_id"], user.household_id)
    for key, value in data.items():
        setattr(goal, key, value)
    db.commit()
    db.refresh(goal)
    return goals_service.compute_goal(db, _household(db, user), goal)


@router.delete("/{goal_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_goal(
    goal_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> Response:
    goal = _get_owned(db, goal_id, user.household_id)
    db.delete(goal)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
