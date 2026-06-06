"""Lightweight metadata endpoint — the running server version, polled by the SPA
to offer a 'reload to update' nudge after a deploy."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import models, schemas
from ..config import get_settings
from ..deps import get_current_user

router = APIRouter(tags=["meta"])
settings = get_settings()


@router.get("/meta", response_model=schemas.MetaOut)
def meta(_user: models.User = Depends(get_current_user)) -> schemas.MetaOut:
    return schemas.MetaOut(version=settings.saiva_version)
