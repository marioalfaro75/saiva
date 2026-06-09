"""In-app alerts feed, preferences, a test email, and the cron run endpoint
(PRD R34/R35). Email is opt-in and configured via the environment (SMTP_*)."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import notifications as svc
from ..services.mailer import send_email

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=schemas.NotificationListOut)
def list_notifications(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> schemas.NotificationListOut:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    svc.generate(db, household)  # refresh the feed on read (idempotent)
    rows = (
        db.execute(
            select(models.Notification)
            .where(models.Notification.household_id == user.household_id)
            .order_by(models.Notification.created_at.desc())
            .limit(100)
        )
        .scalars()
        .all()
    )
    unread = sum(1 for n in rows if n.read_at is None)
    return schemas.NotificationListOut(
        items=[schemas.NotificationOut.model_validate(n) for n in rows], unread=unread
    )


def _owned(db: Session, note_id: str, household_id: str) -> models.Notification:
    note = db.get(models.Notification, note_id)
    if note is None or note.household_id != household_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    return note


@router.post("/read-all", response_model=schemas.MessageOut)
def mark_all_read(
    user: models.User = Depends(require_writer), db: Session = Depends(get_db)
) -> schemas.MessageOut:
    now = dt.datetime.utcnow()
    rows = (
        db.execute(
            select(models.Notification).where(
                models.Notification.household_id == user.household_id,
                models.Notification.read_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for n in rows:
        n.read_at = now
    db.commit()
    return schemas.MessageOut(message=f"Marked {len(rows)} read")


@router.post("/{note_id}/read", response_model=schemas.NotificationOut)
def mark_read(
    note_id: str,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> models.Notification:
    note = _owned(db, note_id, user.household_id)
    if note.read_at is None:
        note.read_at = dt.datetime.utcnow()
        db.commit()
        db.refresh(note)
    return note


@router.get("/settings", response_model=schemas.NotificationSettingsOut)
def get_settings_endpoint(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> schemas.NotificationSettingsOut:
    ns = svc.settings_for(db, user.household_id)
    return schemas.NotificationSettingsOut(
        email_enabled=ns.email_enabled,
        digest=ns.digest,
        large_txn_threshold_cents=ns.large_txn_threshold_cents,
        low_balance_threshold_cents=ns.low_balance_threshold_cents,
        smtp_configured=get_settings().smtp_configured,
    )


@router.patch("/settings", response_model=schemas.NotificationSettingsOut)
def update_settings(
    payload: schemas.NotificationSettingsUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.NotificationSettingsOut:
    ns = svc.settings_for(db, user.household_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(ns, key, value)
    db.commit()
    db.refresh(ns)
    return schemas.NotificationSettingsOut(
        email_enabled=ns.email_enabled,
        digest=ns.digest,
        large_txn_threshold_cents=ns.large_txn_threshold_cents,
        low_balance_threshold_cents=ns.low_balance_threshold_cents,
        smtp_configured=get_settings().smtp_configured,
    )


@router.post("/test", response_model=schemas.MessageOut)
def send_test_email(
    user: models.User = Depends(require_writer), db: Session = Depends(get_db)
) -> schemas.MessageOut:
    settings = get_settings()
    if not settings.smtp_configured:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "SMTP is not configured (set SMTP_* env)")
    sent = send_email([user.email], "Saiva test email", "This is a test email from Saiva.")
    if not sent:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Could not send the test email")
    return schemas.MessageOut(message=f"Test email sent to {user.email}")


@router.post("/run", response_model=schemas.NotificationRunOut)
def run(
    x_notify_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> schemas.NotificationRunOut:
    token = get_settings().notifications_token
    if not token or x_notify_token != token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid notifications token")
    totals = svc.run_all(db)
    return schemas.NotificationRunOut(**totals)
