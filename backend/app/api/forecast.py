"""Cashflow forecasting with simple what-if scenarios (PRD R27)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, optional_period
from ..services import forecast as forecast_service
from ..services import periods

router = APIRouter(prefix="/forecast", tags=["forecast"])


@router.post("", response_model=schemas.ForecastOut)
def post_forecast(
    payload: schemas.ForecastRequest | None = None,
    window: periods.ResolvedPeriod | None = Depends(optional_period),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.ForecastOut:
    payload = payload or schemas.ForecastRequest()
    adjustments = {a.category_id: a.pct for a in payload.adjustments}
    # Projecting from the selected period's vantage point: for a past period that is
    # how the months after it were tracking, rather than a forecast from today.
    result = forecast_service.forecast(
        db,
        user.household_id,
        days=payload.days,
        adjustments=adjustments,
        today=window.as_at if window else None,
    )
    return schemas.ForecastOut.model_validate(result)
