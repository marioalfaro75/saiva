"""AI advisor: BYO provider settings and an ask-your-data chat (PRD §10)."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import models, schemas
from ..db import get_db
from ..deps import get_current_user, require_writer
from ..services import advisor, crypto
from ..services import audit as audit_service

router = APIRouter(prefix="/ai", tags=["ai"])


def _to_out(ai: models.AiSettings) -> schemas.AiSettingsOut:
    return schemas.AiSettingsOut(
        provider=ai.provider,
        base_url=ai.base_url,
        model=ai.model,
        privacy_mode=ai.privacy_mode,
        has_key=bool(ai.api_key_encrypted),
        configured=ai.provider in ("anthropic", "openai"),
    )


@router.get("/settings", response_model=schemas.AiSettingsOut)
def get_ai_settings(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> schemas.AiSettingsOut:
    return _to_out(advisor.settings_for(db, user.household_id))


@router.patch("/settings", response_model=schemas.AiSettingsOut)
def update_ai_settings(
    payload: schemas.AiSettingsUpdate,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.AiSettingsOut:
    ai = advisor.settings_for(db, user.household_id)
    data = payload.model_dump(exclude_unset=True)
    api_key = data.pop("api_key", None)
    for key, value in data.items():
        setattr(ai, key, value)
    if api_key is not None:
        ai.api_key_encrypted = crypto.encrypt(api_key) if api_key else None
    db.commit()
    db.refresh(ai)
    return _to_out(ai)


@router.post("/chat", response_model=schemas.ChatReply)
def chat(
    payload: schemas.ChatRequest,
    user: models.User = Depends(require_writer),
    db: Session = Depends(get_db),
) -> schemas.ChatReply:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    ai = advisor.settings_for(db, user.household_id)
    messages = [{"role": m.role, "content": m.content} for m in payload.messages]
    try:
        reply = advisor.chat(db, household, messages)
    except advisor.NotConfiguredError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except advisor.ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI provider error: {exc}") from exc
    except httpx.HTTPError as exc:  # connection/timeout, no response body
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach the AI provider: {exc}"
        ) from exc
    # Local audit of AI calls (PRD R42) — record the call, never the data.
    audit_service.record(
        db,
        action="ai_chat",
        household_id=user.household_id,
        actor_user_id=user.id,
        detail={"provider": ai.provider, "privacy_mode": ai.privacy_mode, "turns": len(messages)},
    )
    return schemas.ChatReply(reply=reply)


def _require_configured(db: Session, household_id: str) -> models.AiSettings:
    ai = advisor.settings_for(db, household_id)
    if ai.provider not in ("anthropic", "openai"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Configure a provider and API key first")
    return ai


@router.get("/models", response_model=list[schemas.AiModelOut])
def list_ai_models(
    user: models.User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[schemas.AiModelOut]:
    ai = _require_configured(db, user.household_id)
    try:
        found = advisor.list_models(ai)
    except advisor.ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI provider error: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach the AI provider: {exc}"
        ) from exc
    return [schemas.AiModelOut(id=m["id"], label=m["label"]) for m in found]


@router.post("/test", response_model=schemas.MessageOut)
def test_ai_connection(
    user: models.User = Depends(require_writer), db: Session = Depends(get_db)
) -> schemas.MessageOut:
    ai = _require_configured(db, user.household_id)
    try:
        advisor.test_connection(ai)
    except advisor.ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"AI provider error: {exc}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"Could not reach the AI provider: {exc}"
        ) from exc
    return schemas.MessageOut(message="Connected — the model responded.")
