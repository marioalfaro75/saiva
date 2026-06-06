"""Administration: data export (no lock-in, PRD R33), demo seeding, audit log."""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import get_settings
from ..db import get_db
from ..deps import require_owner, require_writer
from ..services import audit, updates
from ..services.seed import create_demo_data

router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()


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


@router.get("/update-check", response_model=schemas.UpdateStatus)
def update_check(
    force: bool = False, _user: models.User = Depends(require_owner)
) -> schemas.UpdateStatus:
    current = settings.saiva_version
    apply_available = bool(settings.watchtower_url and settings.watchtower_token)
    if not settings.update_check_enabled:
        return schemas.UpdateStatus(
            current_version=current, latest_version=None, update_available=False,
            apply_available=apply_available, check_enabled=False,
        )
    release = updates.latest_release_cached(settings.update_repo, force=force)
    if release is None:
        return schemas.UpdateStatus(
            current_version=current, latest_version=None, update_available=False,
            apply_available=apply_available, check_enabled=True,
        )
    return schemas.UpdateStatus(
        current_version=current,
        latest_version=release.tag,
        update_available=updates.is_newer(release.tag, current),
        apply_available=apply_available,
        check_enabled=True,
        release_url=release.url,
        release_notes=release.notes,
        published_at=release.published_at,
    )


@router.post("/update", status_code=status.HTTP_202_ACCEPTED)
def run_update(
    user: models.User = Depends(require_owner), db: Session = Depends(get_db)
) -> dict[str, str]:
    if not (settings.watchtower_url and settings.watchtower_token):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "In-app update isn't configured for this deployment — update with `make pull`.",
        )
    # Fire-and-forget: Watchtower pulls new images and recreates the api/web
    # containers (replacing this very process), so we don't block on the result.
    threading.Thread(
        target=updates.trigger_watchtower,
        args=(settings.watchtower_url, settings.watchtower_token),
        daemon=True,
    ).start()
    audit.record(db, action="update_triggered", household_id=user.household_id,
                 actor_user_id=user.id)
    return {"status": "update_started"}
