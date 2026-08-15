"""Overview dashboard endpoints (PRD R17–R19)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, resolved_period
from ..services import dashboard, periods

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _household(db: Session, user: models.User) -> models.Household:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return household


@router.get("/summary", response_model=schemas.SummaryOut)
def summary(
    window: periods.ResolvedPeriod = Depends(resolved_period),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.SummaryOut:
    return dashboard.summary(db, _household(db, user), "custom", window.start, window.end)


@router.get("/categories", response_model=schemas.CategoryBreakdownOut)
def categories(
    window: periods.ResolvedPeriod = Depends(resolved_period),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.CategoryBreakdownOut:
    return dashboard.category_breakdown(
        db, _household(db, user), "custom", window.start, window.end
    )


@router.get("/trends", response_model=schemas.TrendOut)
def trends(
    window: periods.ResolvedPeriod = Depends(resolved_period),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.TrendOut:
    return dashboard.trends(db, _household(db, user), "custom", window.start, window.end)
