"""FastAPI application: middleware (CSRF + security headers + CORS) and routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import api_router
from .config import get_settings
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
def health() -> dict[str, str]:
    return {"status": "ok"}
