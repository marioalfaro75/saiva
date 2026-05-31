"""Very small in-memory sliding-window limiter for sensitive endpoints (login)."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from .config import get_settings

settings = get_settings()
_hits: dict[str, deque[float]] = defaultdict(deque)


def rate_limit_login(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    window = 60.0
    bucket = _hits[ip]
    while bucket and now - bucket[0] > window:
        bucket.popleft()
    if len(bucket) >= settings.rate_limit_login_per_minute:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; please wait.")
    bucket.append(now)
