import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.code_importers import CodeImportersResponse
from app.services.code_importers import IMPORTERS_LIMIT, code_importers
from app.services.file_content import FileContentError
from app.services.rate_limit import RateLimit

# Python files that import one Python file (reverse of /files/imports); requires an
# authenticated user and goes through the existing ownership lookup.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/files/importers", response_model=CodeImportersResponse)
async def get_file_importers(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
    db: DbSession,
    user: CurrentUser,
    limiter: RateLimit,
) -> CodeImportersResponse:
    # Ownership first (404 "Repository not found"), then the per-user limit (429).
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    await limiter.hit(IMPORTERS_LIMIT, str(user.id))
    try:
        return await code_importers(db, repository, path)
    except FileContentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
