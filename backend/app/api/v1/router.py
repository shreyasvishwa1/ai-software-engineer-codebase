"""Aggregates every API v1 route module into a single router.

``app.main`` mounts ``api_router`` under ``settings.api_prefix`` (default ``/api``),
so routes registered here are served at ``/api/v1/...``.
"""

from fastapi import APIRouter

from app.api.v1.routes import analysis_jobs, auth, health, ingestion, projects, repositories

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(repositories.router)
api_router.include_router(ingestion.router)
api_router.include_router(analysis_jobs.router)
