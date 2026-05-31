"""Household settings (locale/currency/FY/pay-cycle) and family-member management."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas, security
from ..db import get_db
from ..deps import get_current_user, require_owner, require_writer

router = APIRouter(prefix="/household", tags=["household"])


@router.get("", response_model=schemas.HouseholdOut)
def get_household(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> models.Household:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return household


@router.patch("", response_model=schemas.HouseholdOut)
def update_household(
    payload: schemas.HouseholdUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> models.Household:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(household, key, value)
    db.commit()
    db.refresh(household)
    return household


@router.get("/users", response_model=list[schemas.UserOut])
def list_users(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[models.User]:
    return list(
        db.execute(
            select(models.User).where(models.User.household_id == user.household_id)
        )
        .scalars()
        .all()
    )


@router.post("/users", response_model=schemas.UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: schemas.UserCreate,
    owner: models.User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> models.User:
    if db.execute(
        select(models.User.id).where(models.User.email == payload.email.lower())
    ).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already in use")
    member = models.User(
        household_id=owner.household_id,
        email=payload.email.lower(),
        name=payload.name,
        role=payload.role,
        password_hash=security.hash_password(payload.password),
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return member
