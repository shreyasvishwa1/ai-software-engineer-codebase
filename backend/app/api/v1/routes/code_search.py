import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.code_search import CodeSearchRequest, CodeSearchResponse
from app.services.code_search import CODE_SEARCH_LIMIT, code_search
from app.services.rate_limit import RateLimit

# Literal text search; requires an authenticated user and goes through the existing
# ownership lookup. The query travels in the body so it never appears in URLs or access logs.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}/search",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("", response_model=CodeSearchResponse)
async def search_repository(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    body: CodeSearchRequest,
    db: DbSession,
    user: CurrentUser,
    limiter: RateLimit,
) -> CodeSearchResponse:
    # Ownership first (404 "Repository not found"), then the per-user limit (429).
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    await limiter.hit(CODE_SEARCH_LIMIT, str(user.id))
    return await code_search(db, repository, body.query)
