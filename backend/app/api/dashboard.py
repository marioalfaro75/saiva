"""Overview dashboard endpoints (PRD R17–R19)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user
from ..services import dashboard

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _household(db: Session, user: models.User) -> models.Household:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return household


@router.get("/summary", response_model=schemas.SummaryOut)
def summary(
    period: str = "this_fy",
    start: dt.date | None = None,
    end: dt.date | None = None,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.SummaryOut:
    return dashboard.summary(db, _household(db, user), period, start, end)


@router.get("/categories", response_model=schemas.CategoryBreakdownOut)
def categories(
    period: str = "this_fy",
    start: dt.date | None = None,
    end: dt.date | None = None,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.CategoryBreakdownOut:
    return dashboard.category_breakdown(db, _household(db, user), period, start, end)


@router.get("/trends", response_model=schemas.TrendOut)
def trends(
    period: str = "this_fy",
    start: dt.date | None = None,
    end: dt.date | None = None,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.TrendOut:
    return dashboard.trends(db, _household(db, user), period, start, end)
