"""Append-only audit log for logins and destructive/admin actions (PRD §15)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from .. import models


def record(
    db: Session,
    *,
    action: str,
    household_id: str | None = None,
    actor_user_id: str | None = None,
    entity: str | None = None,
    entity_id: str | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    db.add(
        models.AuditLog(
            action=action,
            household_id=household_id,
            actor_user_id=actor_user_id,
            entity=entity,
            entity_id=entity_id,
            ip=ip,
            detail=detail,
        )
    )
    db.commit()
