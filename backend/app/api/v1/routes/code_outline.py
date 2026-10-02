import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.code_outline import CodeOutlineResponse
from app.services.code_outline import OUTLINE_LIMIT, code_outline
from app.services.file_content import FileContentError
from app.services.rate_limit import RateLimit

# Structural outline of one Python file; requires an authenticated user and goes through the
# existing ownership lookup. Limits are fixed server-side.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/files/outline", response_model=CodeOutlineResponse, response_model_by_alias=True)
async def get_file_outline(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
    db: DbSession,
    user: CurrentUser,
    limiter: RateLimit,
) -> CodeOutlineResponse:
    # Ownership first (404 "Repository not found"), then the per-user limit (429).
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    await limiter.hit(OUTLINE_LIMIT, str(user.id))
    try:
        return await code_outline(db, repository, path)
    except FileContentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
