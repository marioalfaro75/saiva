"""Authentication: first-run setup, login/logout, session/CSRF, current user."""

from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models, schemas, security
from ..clientip import client_ip
from ..config import get_settings
from ..db import get_db
from ..deps import get_current_user
from ..ratelimit import rate_limit_login
from ..services import audit
from ..services.seed import seed_household_defaults

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

# Hashed once at import from a value nobody knows, purely so the no-such-user path can
# do the same work the real one does.
_TIMING_EQUALISER = security.hash_password(secrets.token_urlsafe(32))


def _client_ip(request: Request) -> str | None:
    # Resolved through the trusted-proxy rules rather than read straight off the
    # socket: behind Caddy the peer is always Caddy, so every audit row used to
    # record one container IP and told you nothing about who did the thing.
    return client_ip(request)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        security.SESSION_COOKIE, token,
        httponly=True, secure=settings.cookie_secure, samesite=settings.cookie_samesite,
        max_age=settings.session_ttl_minutes * 60, path="/",
    )


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        security.CSRF_COOKIE, token,
        httponly=False, secure=settings.cookie_secure, samesite=settings.cookie_samesite,
        max_age=settings.session_ttl_minutes * 60, path="/",
    )


def _me(user: models.User, household: models.Household, csrf_token: str) -> schemas.MeOut:
    return schemas.MeOut(
        user=schemas.UserOut.model_validate(user),
        household=schemas.HouseholdOut.model_validate(household),
        csrf_token=csrf_token,
    )


@router.get("/status")
def auth_status(db: Session = Depends(get_db)) -> dict[str, bool]:
    initialised = db.execute(select(models.User.id).limit(1)).first() is not None
    return {"initialised": initialised}


@router.get("/csrf")
def issue_csrf(request: Request, response: Response) -> dict[str, str]:
    """Hand back the caller's CSRF token, minting one only if they have none.

    Rotating it on every call breaks any other tab that is already open: that tab
    keeps sending the token it was given, which no longer matches the cookie, and
    every write it attempts fails until it is reloaded.
    """
    token = request.cookies.get(security.CSRF_COOKIE) or security.generate_csrf_token()
    _set_csrf_cookie(response, token)
    return {"csrf_token": token}


@router.post("/setup", response_model=schemas.MeOut, status_code=status.HTTP_201_CREATED)
def setup(
    payload: schemas.SetupRequest,
    request: Request,
    response: Response,
    _rl: None = Depends(rate_limit_login),
    db: Session = Depends(get_db),
) -> schemas.MeOut:
    if db.execute(select(models.User.id).limit(1)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Already initialised")

    household = models.Household(
        name=payload.household_name, state=payload.state, period_basis=payload.period_basis
    )
    db.add(household)
    db.flush()
    seed_household_defaults(db, household)

    user = models.User(
        household_id=household.id, email=payload.email.lower(), name=payload.name,
        role="owner", password_hash=security.hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.refresh(household)

    _set_session_cookie(response, security.create_session_token(user.id, user.session_epoch))
    csrf = security.generate_csrf_token()
    _set_csrf_cookie(response, csrf)
    audit.record(db, action="setup", household_id=household.id, actor_user_id=user.id,
                 ip=_client_ip(request))
    return _me(user, household, csrf)


@router.post("/login", response_model=schemas.MeOut)
def login(
    payload: schemas.LoginRequest,
    request: Request,
    response: Response,
    _rl: None = Depends(rate_limit_login),
    db: Session = Depends(get_db),
) -> schemas.MeOut:
    email = payload.email.lower()
    user = db.execute(select(models.User).where(models.User.email == email)).scalar_one_or_none()
    if user is None or not user.is_active:
        # Verify against a throwaway hash so an unknown address costs the same Argon2
        # work as a known one. Without this the reply comes back immediately, and the
        # gap is wide enough to enumerate which addresses have accounts.
        security.verify_password(_TIMING_EQUALISER, payload.password)
        audit.record(db, action="login_failed", ip=_client_ip(request), detail={"email": email})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if not security.verify_password(user.password_hash, payload.password):
        audit.record(db, action="login_failed", ip=_client_ip(request), detail={"email": email})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    if security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(payload.password)
    user.last_login_at = dt.datetime.utcnow()
    db.commit()
    db.refresh(user)
    household = db.get(models.Household, user.household_id)
    assert household is not None

    _set_session_cookie(response, security.create_session_token(user.id, user.session_epoch))
    csrf = security.generate_csrf_token()
    _set_csrf_cookie(response, csrf)
    audit.record(db, action="login", household_id=user.household_id, actor_user_id=user.id,
                 ip=_client_ip(request))
    return _me(user, household, csrf)


@router.post("/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    response.delete_cookie(security.CSRF_COOKIE, path="/")
    return {"message": "Signed out"}


@router.post("/password", response_model=schemas.MessageOut)
def change_password(
    payload: schemas.PasswordChange,
    request: Request,
    response: Response,
    _rl: None = Depends(rate_limit_login),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.MessageOut:
    """Change a password, ending every other session.

    There was no way to change a password at all, and signing out only cleared the
    browser's cookie while the token stayed valid for its full term — so a household
    that suspected a password was known had no move available. Raising the epoch is
    what makes the change mean something: tokens issued before it stop working.
    """
    if not security.verify_password(user.password_hash, payload.current_password):
        audit.record(db, action="password_change_failed", household_id=user.household_id,
                     actor_user_id=user.id, ip=_client_ip(request))
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Current password is incorrect")

    user.password_hash = security.hash_password(payload.new_password)
    user.session_epoch += 1
    db.commit()
    db.refresh(user)
    # This browser keeps working; every other one is signed out.
    _set_session_cookie(response, security.create_session_token(user.id, user.session_epoch))
    audit.record(db, action="password_changed", household_id=user.household_id,
                 actor_user_id=user.id, ip=_client_ip(request))
    return schemas.MessageOut(message="Password changed. Other devices have been signed out.")


@router.post("/sign-out-everywhere", response_model=schemas.MessageOut)
def sign_out_everywhere(
    request: Request,
    response: Response,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.MessageOut:
    """End every session for this account, including this one."""
    user.session_epoch += 1
    db.commit()
    audit.record(db, action="sign_out_everywhere", household_id=user.household_id,
                 actor_user_id=user.id, ip=_client_ip(request))
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    response.delete_cookie(security.CSRF_COOKIE, path="/")
    return schemas.MessageOut(message="Signed out on every device.")


@router.get("/me", response_model=schemas.MeOut)
def me(
    request: Request,
    response: Response,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> schemas.MeOut:
    household = db.get(models.Household, user.household_id)
    assert household is not None
    csrf = request.cookies.get(security.CSRF_COOKIE) or security.generate_csrf_token()
    _set_csrf_cookie(response, csrf)
    return _me(user, household, csrf)
