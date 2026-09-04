"""In-memory sliding-window rate limiting for the endpoints worth protecting.

Two things were wrong with limiting login alone. Password guessing is not the only
thing an unauthenticated or low-privilege caller can do repeatedly — password
change, session revocation, file import and the AI advisor all cost far more per
request than a login does, and none of them was limited. And the bucket dictionary
grew a new entry per address forever, so the limiter itself was a slow memory leak
reachable by anyone who could vary a source address.
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque

from fastapi import HTTPException, Request, status

from .clientip import client_ip
from .config import get_settings

settings = get_settings()

WINDOW_SECONDS = 60.0

# The store is bounded: past this many distinct callers the least recently seen are
# dropped. Eviction can only ever forgive requests, never invent them, so the worst
# case under a flood of spoofed keys is that the limiter stops helping — not that
# the process runs out of memory.
MAX_TRACKED_KEYS = 4096

_hits: OrderedDict[str, deque[float]] = OrderedDict()


def _record(key: str, limit: int) -> bool:
    """Register a hit; return whether it is within the limit."""
    now = time.time()
    bucket = _hits.get(key)
    if bucket is None:
        bucket = deque()
        _hits[key] = bucket
    _hits.move_to_end(key)

    while bucket and now - bucket[0] > WINDOW_SECONDS:
        bucket.popleft()

    while len(_hits) > MAX_TRACKED_KEYS:
        _hits.popitem(last=False)

    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def reset() -> None:
    """Drop all state. For tests — the limiter is otherwise process-lifetime."""
    _hits.clear()


class RateLimit:
    """A FastAPI dependency limiting one named group of endpoints per caller.

    Each name gets its own bucket, so hammering the importer cannot lock a
    household out of logging in, and vice versa.
    """

    def __init__(self, name: str, per_minute: int) -> None:
        self.name = name
        self.per_minute = per_minute

    def __call__(self, request: Request) -> None:
        if self.per_minute <= 0:  # 0 disables the limit
            return
        caller = client_ip(request) or "unknown"
        if not _record(f"{self.name}:{caller}", self.per_minute):
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many requests; please wait a moment and try again.",
                headers={"Retry-After": str(int(WINDOW_SECONDS))},
            )


def rate_limit_login(request: Request) -> None:
    """Credential endpoints: login, first-run setup, password change."""
    RateLimit("credentials", settings.rate_limit_login_per_minute)(request)


def rate_limit_import(request: Request) -> None:
    """Parsing and committing a statement file; megabytes of work per call."""
    RateLimit("import", settings.rate_limit_import_per_minute)(request)


def rate_limit_ai(request: Request) -> None:
    """Advisor calls leave the machine and, on a paid provider, cost real money."""
    RateLimit("ai", settings.rate_limit_ai_per_minute)(request)


def rate_limit_report(request: Request) -> None:
    """PDF and CSV generation reads and renders the whole period."""
    RateLimit("report", settings.rate_limit_report_per_minute)(request)
