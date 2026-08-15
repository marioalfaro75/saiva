"""Reusable FastAPI dependencies: authentication and role enforcement."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models, security
from .constants import ROLE_RANK
from .db import get_db
from .services import periods


def get_current_user(request: Request, db: Session = Depends(get_db)) -> models.User:
    token = request.cookies.get(security.SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    user_id = security.decode_session_token(token)
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    user = db.get(models.User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User not found or inactive")
    return user


def require_min_role(minimum: str) -> Callable[..., models.User]:
    """Dependency factory: require at least `minimum` role (viewer < adult < owner)."""

    def checker(user: models.User = Depends(get_current_user)) -> models.User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK[minimum]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return user

    return checker


# Convenience dependencies.
require_writer = require_min_role("adult")  # full data access, no destructive admin
require_owner = require_min_role("owner")


def get_household(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> models.Household:
    household = db.get(models.Household, user.household_id)
    if household is None:  # pragma: no cover - a session always belongs to a household
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Household not found")
    return household


def data_bounds(db: Session, household_id: str) -> tuple[dt.date, dt.date] | None:
    """Earliest and latest transaction dates, backing the "All time" selector."""
    first, last = db.execute(
        select(func.min(models.Transaction.txn_date), func.max(models.Transaction.txn_date)).where(
            models.Transaction.household_id == household_id
        )
    ).one()
    return (first, last) if first and last else None


def _resolve(
    household: models.Household,
    db: Session,
    period: str,
    start: dt.date | None,
    end: dt.date | None,
) -> periods.ResolvedPeriod:
    bounds = data_bounds(db, household.id) if period == "all" else None
    try:
        return periods.resolve(household, period, start, end, all_bounds=bounds)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def resolved_period(
    period: str = "this_fy",
    start: dt.date | None = None,
    end: dt.date | None = None,
    household: models.Household = Depends(get_household),
    db: Session = Depends(get_db),
) -> periods.ResolvedPeriod:
    """The window every period-aware endpoint works in, resolved one way for all of
    them so the app's global period picker means the same thing everywhere."""
    return _resolve(household, db, period, start, end)


def optional_period(
    period: str | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
    household: models.Household = Depends(get_household),
    db: Session = Depends(get_db),
) -> periods.ResolvedPeriod | None:
    """As `resolved_period`, but absent when no period was asked for — so endpoints
    that already had their own date filters behave exactly as before without one."""
    if not period:
        return None
    return _resolve(household, db, period, start, end)
