"""API router aggregation (mounted at /api in app.main)."""

from fastapi import APIRouter

from . import (
    accounts,
    admin,
    auth,
    categories,
    dashboard,
    households,
    imports,
    transactions,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(households.router)
api_router.include_router(accounts.router)
api_router.include_router(categories.router)
api_router.include_router(imports.router)
api_router.include_router(transactions.router)
api_router.include_router(dashboard.router)
api_router.include_router(admin.router)
