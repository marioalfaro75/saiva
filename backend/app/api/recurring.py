"""Recurring & subscription detection and the upcoming-bills view (PRD R16/R26)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user
from ..services import recurring as recurring_service

router = APIRouter(prefix="/recurring", tags=["recurring"])


@router.get("", response_model=schemas.RecurringOut)
def get_recurring(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> schemas.RecurringOut:
    series = recurring_service.detect(db, user.household_id)
    active = [s for s in series if s.active]
    subs = [s for s in active if s.is_subscription]
    return schemas.RecurringOut(
        series=[schemas.RecurringSeriesOut.model_validate(s) for s in series],
        monthly_committed_cents=sum(
            s.monthly_amount_cents for s in active if s.direction == "expense"
        ),
        subscriptions_count=len(subs),
        subscriptions_monthly_cents=sum(s.monthly_amount_cents for s in subs),
        income_monthly_cents=sum(
            s.monthly_amount_cents for s in active if s.direction == "income"
        ),
    )


@router.get("/upcoming", response_model=schemas.UpcomingBillsOut)
def get_upcoming_bills(
    days: int = Query(60, ge=1, le=365),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.UpcomingBillsOut:
    bills = recurring_service.upcoming_bills(db, user.household_id, days=days)
    return schemas.UpcomingBillsOut(
        horizon_days=days,
        total_cents=sum(b.amount_cents for b in bills),
        bills=[schemas.UpcomingBillOut.model_validate(b) for b in bills],
    )
