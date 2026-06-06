"""API router aggregation (mounted at /api in app.main)."""

from fastapi import APIRouter

from . import (
    accounts,
    admin,
    auth,
    budgets,
    categories,
    dashboard,
    goals,
    households,
    imports,
    meta,
    networth,
    transactions,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(meta.router)
api_router.include_router(households.router)
api_router.include_router(accounts.router)
api_router.include_router(categories.router)
api_router.include_router(imports.router)
api_router.include_router(transactions.router)
api_router.include_router(dashboard.router)
api_router.include_router(budgets.router)
api_router.include_router(networth.router)
api_router.include_router(goals.router)
api_router.include_router(admin.router)
