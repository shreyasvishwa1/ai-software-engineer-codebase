"""Aggregates every API v1 route module into a single router.

``app.main`` mounts ``api_router`` under ``settings.api_prefix`` (default ``/api``),
so routes registered here are served at ``/api/v1/...``.
"""

from fastapi import APIRouter

from app.api.v1.routes import (
    analysis_jobs,
    auth,
    code_call_hierarchy,
    code_definition,
    code_importers,
    code_imports,
    code_method_overrides,
    code_metrics,
    code_occurrences,
    code_outline,
    code_references,
    code_replaced_definitions,
    code_search,
    code_type_hierarchy,
    code_unbound_names,
    code_unused_imports,
    code_unused_variables,
    dependencies,
    file_explanation,
    health,
    ingestion,
    projects,
    repositories,
    repository_files,
    secret_scan,
    symbol_search,
)

api_router = APIRouter(prefix="/v1")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(projects.router)
api_router.include_router(repositories.router)
api_router.include_router(ingestion.router)
api_router.include_router(analysis_jobs.router)
api_router.include_router(repository_files.router)
api_router.include_router(code_metrics.router)
api_router.include_router(dependencies.router)
api_router.include_router(file_explanation.router)
api_router.include_router(secret_scan.router)
api_router.include_router(code_search.router)
api_router.include_router(code_outline.router)
api_router.include_router(code_imports.router)
api_router.include_router(code_importers.router)
api_router.include_router(symbol_search.router)
api_router.include_router(code_definition.router)
api_router.include_router(code_references.router)
api_router.include_router(code_call_hierarchy.router)
api_router.include_router(code_type_hierarchy.router)
api_router.include_router(code_method_overrides.router)
api_router.include_router(code_unbound_names.router)
api_router.include_router(code_occurrences.router)
api_router.include_router(code_unused_variables.router)
api_router.include_router(code_unused_imports.router)
api_router.include_router(code_replaced_definitions.router)
