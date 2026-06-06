"""ABS spending benchmarks: indicative typical-vs-yours comparison (Phase 2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user
from ..services import benchmarks as benchmarks_service

router = APIRouter(prefix="/benchmarks", tags=["benchmarks"])


@router.get("", response_model=schemas.BenchmarkOut)
def get_benchmarks(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> schemas.BenchmarkOut:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return benchmarks_service.benchmark(db, household)
