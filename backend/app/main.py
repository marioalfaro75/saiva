"""FastAPI application: middleware (CSRF + security headers + CORS) and routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from .api import api_router
from .config import get_settings
from .db import engine as db_engine
from .schema_state import read_schema_state
from .security import CSRF_COOKIE, CSRF_HEADER, csrf_valid

settings = get_settings()
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Endpoints authenticated by a header the caller must know, not by an ambient cookie.
# CSRF exists because a browser attaches cookies to cross-site requests without being
# asked; a request that must carry a secret header cannot be forged that way, and there
# is nothing for the double-submit check to protect. Requiring it here only broke the
# documented cron integration, which could never send a CSRF cookie it was never given.
CSRF_EXEMPT_PATHS = frozenset({"/api/notifications/run"})

# Slightly above the import cap, to leave room for multipart framing around a
# maximum-size file rather than rejecting a legitimate one on overhead.
MAX_REQUEST_BYTES = 12 * 1024 * 1024

app = FastAPI(
    title="Saiva API",
    version=settings.saiva_version,
    docs_url=None if settings.is_production else "/api/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/api/openapi.json",
)

if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*", CSRF_HEADER],
    )


@app.middleware("http")
async def security_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    # Double-submit CSRF protection for state-changing API calls.
    if (
        request.method not in SAFE_METHODS
        and request.url.path.startswith("/api")
        and request.url.path not in CSRF_EXEMPT_PATHS
        and not csrf_valid(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER))
    ):
        return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)

    # Refuse an oversized body on its declared length, before anything reads it. The
    # route-level cap still applies — a client can lie about Content-Length — but this
    # turns the common case into a rejection at the door.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        return JSONResponse({"detail": "Request too large"}, status_code=413)

    response = await call_next(request)

    path = request.url.path
    is_docs = path.startswith("/api/docs") or path == "/api/openapi.json"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if not is_docs:
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.include_router(api_router, prefix="/api")


@app.get("/api/health", tags=["meta"])
def health(response: Response) -> dict[str, str | None]:
    """What this process can actually see, reported as data rather than as a verdict.

    This used to be `return {"status": "ok"}` — a literal, meaning only "uvicorn is
    listening". It is the single signal consumed by the image's HEALTHCHECK, by
    scripts/deploy.sh's wait loop and by `compose up --wait`, so every gate in the
    deployment resolved to that unconditional 200. During the outage it printed a tick
    while the site was unreachable from the LAN.

    Two rules, both learned the hard way:

    * Reach the database. A process that is listening but cannot query is not healthy,
      and that is the failure a restart might actually fix.

    * Report the schema state as DATA, and stay 200 for `ahead`. The tempting version of
      this endpoint asserts `current == head` and returns 503 otherwise. Ship that
      alongside the rolled-back-image handling in app/dbinit.py and a rolled-back image
      serves correctly while reporting itself unhealthy: the deploy gate times out,
      declares the rollback a failure and rolls back again, while `restart:
      unless-stopped` loops underneath. A boolean cannot carry a four-valued answer.

    503 is therefore reserved for the one case where this process genuinely cannot do its
    job: the database is unreachable.
    """
    payload: dict[str, str | None] = {
        "status": "ok",
        "version": settings.saiva_version,
        "build": settings.saiva_build or None,
        "database": "ok",
        "schema": None,
        "db_revision": None,
        "image_head": None,
    }
    try:
        with db_engine.connect() as conn:
            conn.execute(sa_text("SELECT 1"))
        payload.update(read_schema_state(db_engine).as_dict())
    except Exception as exc:  # noqa: BLE001 - any failure to reach the DB is the answer
        payload["status"] = "degraded"
        payload["database"] = "error"
        payload["detail"] = type(exc).__name__
        response.status_code = 503
    return payload
