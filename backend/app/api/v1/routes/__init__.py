"""Route modules for API v1.

Convention: one module per resource, each exposing ``router = APIRouter(...)``
with its own ``tags``. Paths are relative to the v1 mount point. Register every
module in ``app.api.v1.router``.
"""
