"""Password hashing (Argon2id), signed session tokens, and CSRF helpers (PRD §15)."""

from __future__ import annotations

import datetime as dt
import hmac
import secrets

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

from .config import get_settings

settings = get_settings()
_ph = PasswordHasher()

SESSION_COOKIE = "saiva_session"
CSRF_COOKIE = "saiva_csrf"
CSRF_HEADER = "X-CSRF-Token"


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except Argon2Error:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


def create_session_token(user_id: str, session_epoch: int = 0) -> str:
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": user_id,
        "epoch": session_epoch,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=settings.session_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_session_token(token: str) -> tuple[str, int] | None:
    """(user id, epoch) from a valid token, or None.

    The epoch is what makes a stateless token revocable: the user row carries the
    current value, and anything stamped with an older one is refused.
    """
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    sub = data.get("sub")
    if not isinstance(sub, str):
        return None
    epoch = data.get("epoch")
    return sub, epoch if isinstance(epoch, int) else 0


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_valid(cookie_value: str | None, header_value: str | None) -> bool:
    if not cookie_value or not header_value:
        return False
    return hmac.compare_digest(cookie_value, header_value)
