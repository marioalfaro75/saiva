"""Financial-year report export (PRD R32): a PDF for the accountant."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user
from ..services import reports as reports_service
from ..services.periods import fy_bounds

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/years", response_model=list[schemas.FYReportOption])
def fy_years(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[schemas.FYReportOption]:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    return [
        schemas.FYReportOption.model_validate(o)
        for o in reports_service.available_years(db, household)
    ]


@router.get("/fy")
def fy_report_pdf(
    year: int | None = Query(default=None, ge=2000, le=2100),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    if year is None:
        year = fy_bounds(household, dt.date.today())[0].year
    report = reports_service.build_fy_report(db, household, year)
    pdf = reports_service.render_pdf(household, report)
    filename = f"{household.name}-{report.label}.pdf".replace(" ", "_")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
