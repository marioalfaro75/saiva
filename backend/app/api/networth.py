"""Net worth: manual assets & liabilities with a daily snapshot trend (Phase 2).

Mutations return the full net-worth view (totals + items + history) so the page
refreshes in one round trip, and each one records today's snapshot."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import networth as networth_service

router = APIRouter(prefix="/net-worth", tags=["net-worth"])


def _get_owned(db: Session, item_id: str, household_id: str) -> models.NetWorthItem:
    item = db.get(models.NetWorthItem, item_id)
    if item is None or item.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    return item


@router.get("", response_model=schemas.NetWorthOut)
def get_net_worth(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> schemas.NetWorthOut:
    return networth_service.get_net_worth(db, user.household_id)


@router.post("/items", response_model=schemas.NetWorthOut, status_code=status.HTTP_201_CREATED)
def create_item(
    payload: schemas.NetWorthItemCreate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.NetWorthOut:
    db.add(models.NetWorthItem(household_id=user.household_id, **payload.model_dump()))
    db.commit()
    networth_service.record_snapshot(db, user.household_id)
    return networth_service.get_net_worth(db, user.household_id)


@router.patch("/items/{item_id}", response_model=schemas.NetWorthOut)
def update_item(
    item_id: str,
    payload: schemas.NetWorthItemUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.NetWorthOut:
    item = _get_owned(db, item_id, user.household_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    db.commit()
    networth_service.record_snapshot(db, user.household_id)
    return networth_service.get_net_worth(db, user.household_id)


@router.delete("/items/{item_id}", response_model=schemas.NetWorthOut)
def delete_item(
    item_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.NetWorthOut:
    item = _get_owned(db, item_id, user.household_id)
    db.delete(item)
    db.commit()
    networth_service.record_snapshot(db, user.household_id)
    return networth_service.get_net_worth(db, user.household_id)


@router.post("/snapshot", response_model=schemas.NetWorthOut)
def snapshot(
    user: models.User = Depends(require_writer), db: Session = Depends(get_db)
) -> schemas.NetWorthOut:
    networth_service.record_snapshot(db, user.household_id)
    return networth_service.get_net_worth(db, user.household_id)
