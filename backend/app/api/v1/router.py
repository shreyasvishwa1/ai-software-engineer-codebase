"""Aggregates every API v1 route module into a single router.

``app.main`` mounts ``api_router`` under ``settings.api_prefix`` (default ``/api``),
so routes registered here are served at ``/api/v1/...``.
"""

from fastapi import APIRouter

from app.api.v1.routes import health

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
