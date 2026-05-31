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

app = FastAPI(
    title="Saiva API",
    version="0.1.0",
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
        and not csrf_valid(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER))
    ):
        return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)

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
