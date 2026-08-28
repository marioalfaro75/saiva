"""Financial-year report export (PRD R32): a PDF for the accountant."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user
from ..services import reports as reports_service
from ..services.periods import fy_bounds

router = APIRouter(prefix="/reports", tags=["reports"])


def _ascii_filename(name: str) -> str:
    """Content-Disposition is latin-1 only, while labels and household names carry
    real typography (an en dash in "FY2025–26", accented letters). Fold those to
    ASCII here rather than restrict what the rest of the app can display."""
    folded = name.replace(" ", "_").replace("–", "-").replace("—", "-")
    ascii_only = (
        unicodedata.normalize("NFKD", folded).encode("ascii", "ignore").decode("ascii")
    )
    # Folding to ASCII is not sanitising: quotes, backslashes and newlines are all
    # ASCII, and this value goes inside a quoted header. A household name is chosen by
    # a member, so `Smith" attack="` would have closed the quoted string and added a
    # header parameter. Keep to characters a filename actually needs.
    safe = re.sub(r"[^A-Za-z0-9._-]", "", ascii_only).lstrip(".")
    return safe or "report.pdf"


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
    filename = _ascii_filename(f"{household.name}-{report.label}.pdf")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
