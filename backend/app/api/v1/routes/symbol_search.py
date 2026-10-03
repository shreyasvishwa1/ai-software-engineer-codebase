import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.symbol_search import SymbolSearchRequest, SymbolSearchResponse
from app.services.file_content import FileContentError
from app.services.rate_limit import RateLimit
from app.services.symbol_search import SYMBOL_SEARCH_LIMIT, symbol_search

# Python symbol search; requires an authenticated user and goes through the existing
# ownership lookup. The query travels in the body so it never appears in URLs or logs.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}/symbols",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("/search", response_model=SymbolSearchResponse)
async def search_symbols(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    body: SymbolSearchRequest,
    db: DbSession,
    user: CurrentUser,
    limiter: RateLimit,
) -> SymbolSearchResponse:
    # Ownership first (404 "Repository not found"), then the per-user limit (429).
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    await limiter.hit(SYMBOL_SEARCH_LIMIT, str(user.id))
    try:
        return await symbol_search(db, repository, body.query)
    except FileContentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
