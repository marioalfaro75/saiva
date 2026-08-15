"""Insights feed: ranked, rule-based cards computed from existing data (Phase 2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, optional_period
from ..services import insights as insights_service
from ..services import periods

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("", response_model=schemas.InsightsOut)
def get_insights(
    window: periods.ResolvedPeriod | None = Depends(optional_period),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.InsightsOut:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return insights_service.generate(db, household, window.as_at if window else None)
