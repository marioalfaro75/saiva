"""Administration: data export (no lock-in, PRD R33), demo seeding, audit log."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
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
    """Whether a newer release exists — and, when that cannot be answered, saying so.

    `check_failed` is the field that was missing. The old shape returned
    `latest_version=None, update_available=False, check_enabled=True` both when the box
    was genuinely up to date and when the request to GitHub had failed outright, and the
    panel rendered "You're on the latest version" for both. A network failure read as
    reassurance.

    `comparable` is the second one. A box running an `edge` or `sha-` build has no
    semver to compare, so "no update available" is not a finding about the box — it is
    the comparison declining to run. `parse_version` returns None for those, `is_newer`
    returns False on None, and the panel used to show a tick.
    """
    current = settings.saiva_version
    common = {
        "current_version": current,
        "current_build": settings.saiva_build or None,
        "apply_available": updates.apply_available(),
        "comparable": updates.parse_version(current) is not None,
    }
    if not settings.update_check_enabled:
        return schemas.UpdateStatus(
            latest_version=None, update_available=False, check_enabled=False,
            check_failed=False, **common,
        )
    release = updates.latest_release_cached(settings.update_repo, force=force)
    if release is None:
        return schemas.UpdateStatus(
            latest_version=None, update_available=False, check_enabled=True,
            check_failed=True, **common,
        )
    return schemas.UpdateStatus(
        latest_version=release.tag,
        update_available=updates.is_newer(release.tag, current),
        check_enabled=True,
        check_failed=False,
        release_url=release.url,
        release_notes=release.notes,
        published_at=release.published_at,
        **common,
    )


@router.post("/update", status_code=status.HTTP_202_ACCEPTED)
def run_update(
    user: models.User = Depends(require_owner), db: Session = Depends(get_db)
) -> dict[str, str]:
    """Ask the host to apply the newest release. Returns once the request is written.

    This endpoint used to start a daemon thread that called Watchtower, because
    Watchtower would destroy the process before it could answer, and it returned the
    literal string "update_started" either way. Now it does one synchronous, verifiable
    thing -- write a file -- and says whether that worked. What HAPPENED to the update
    is a separate question, answered by /update-status from a file on the host that
    outlives this container.
    """
    if not updates.apply_available():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "In-app updates aren't set up on this deployment. Run "
            "`sudo ./scripts/install-updater.sh` on the host, or update with `make update`.",
        )
    if updates.update_in_progress():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An update is already running on this box."
        )
    try:
        request_id = updates.request_update(requested_by=user.email)
    except OSError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Could not ask the host to update: {type(exc).__name__}. "
            "Check that the update directory is writable by the api container.",
        ) from exc
    audit.record(db, action="update_requested", household_id=user.household_id,
                 actor_user_id=user.id)
    return {"status": "requested", "request_id": request_id}


@router.get("/update-status", response_model=schemas.UpdateRun)
def update_status(_user: models.User = Depends(require_owner)) -> schemas.UpdateRun:
    """What the host updater did, read from its record rather than inferred.

    Every field is optional and the panel renders each absent one as a specific unknown.
    That is deliberate: the failure this whole design exists to remove was a UI that
    filled a gap in its knowledge with the word "applied".
    """
    available = updates.apply_available()
    record = updates.read_update_status()
    if record is None:
        return schemas.UpdateRun(phase=None, available=available)
    fields = {
        key: record.get(key)
        for key in schemas.UpdateRun.model_fields
        if key in record and key != "available"
    }
    try:
        return schemas.UpdateRun(available=available, **fields)
    except ValidationError:
        # A status file whose field types do not match -- a half-written render, a
        # hand-edit, a future version of the updater -- must not 500 the settings page.
        # The page exists to report on a broken update; being broken BY one is the one
        # thing it cannot do.
        return schemas.UpdateRun(
            available=available,
            phase="unreadable",
            message="The updater's status file is not in a shape this version understands. "
            "Run ./scripts/deploy.sh --status on the host to see it.",
        )
