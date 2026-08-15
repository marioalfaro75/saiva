"""ABS spending benchmarks: indicative typical-vs-yours comparison (Phase 2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, optional_period
from ..services import benchmarks as benchmarks_service
from ..services import periods

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


@router.get("", response_model=schemas.BenchmarkOut)
def get_benchmarks(
    window: periods.ResolvedPeriod | None = Depends(optional_period),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.BenchmarkOut:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return benchmarks_service.benchmark(db, household, window.as_at if window else None)
