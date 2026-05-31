"""Administration: data export (no lock-in, PRD R33), demo seeding, audit log."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import require_owner, require_writer
from ..services import audit
from ..services.seed import create_demo_data

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/export")
def export_data(
    user: models.User = Depends(require_writer), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Full data export in an open JSON format."""
    hh = db.get(models.Household, user.household_id)
    assert hh is not None

    def rows(model: Any) -> list[dict[str, Any]]:
        objects = (
            db.execute(select(model).where(model.household_id == user.household_id))
            .scalars()
            .all()
        )
        return [
            {c.name: getattr(obj, c.name) for c in model.__table__.columns} for obj in objects
        ]

    return {
        "household": {c.name: getattr(hh, c.name) for c in models.Household.__table__.columns},
        "accounts": rows(models.Account),
        "categories": rows(models.Category),
        "rules": rows(models.CategorisationRule),
        "transactions": rows(models.Transaction),
    }


@router.post("/seed-demo")
def seed_demo(
    user: models.User = Depends(require_owner), db: Session = Depends(get_db)
) -> dict[str, Any]:
    if db.execute(
        select(models.Account.id).where(models.Account.household_id == user.household_id).limit(1)
    ).first():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Household already has accounts; demo seeding skipped."
        )
    household = db.get(models.Household, user.household_id)
    assert household is not None
    count = create_demo_data(db, household)
    audit.record(db, action="seed_demo", household_id=household.id, actor_user_id=user.id,
                 detail={"transactions": count})
    return {"message": "Demo data created", "transactions": count}


@router.get("/audit")
def audit_log(
    user: models.User = Depends(require_owner), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    logs = (
        db.execute(
            select(models.AuditLog)
            .where(models.AuditLog.household_id == user.household_id)
            .order_by(models.AuditLog.created_at.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    return [
        {
            "action": log.action,
            "actor_user_id": log.actor_user_id,
            "entity": log.entity,
            "entity_id": log.entity_id,
            "ip": log.ip,
            "detail": log.detail,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]
