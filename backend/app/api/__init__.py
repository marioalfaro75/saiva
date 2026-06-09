"""API router aggregation (mounted at /api in app.main)."""

from fastapi import APIRouter

from . import (
    accounts,
    admin,
    auth,
    benchmarks,
    budgets,
    categories,
    dashboard,
    forecast,
    goals,
    households,
    imports,
    insights,
    meta,
    networth,
    notifications,
    recurring,
    reports,
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
api_router.include_router(insights.router)
api_router.include_router(benchmarks.router)
api_router.include_router(recurring.router)
api_router.include_router(forecast.router)
api_router.include_router(notifications.router)
api_router.include_router(reports.router)
api_router.include_router(admin.router)
