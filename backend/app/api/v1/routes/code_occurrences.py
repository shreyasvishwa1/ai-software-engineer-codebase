import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.code_occurrences import OccurrencesResponse
from app.services.code_occurrences import OCCURRENCES_LIMIT, code_occurrences
from app.services.file_content import FileContentError
from app.services.rate_limit import RateLimit

# In-file name occurrences for one position of a Python file; requires an authenticated user
# and goes through the existing ownership lookup.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/files/occurrences", response_model=OccurrencesResponse)
async def get_file_occurrences(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
    line: Annotated[int, Query(ge=1)],
    column: Annotated[int, Query(ge=1)],
    db: DbSession,
    user: CurrentUser,
    limiter: RateLimit,
) -> OccurrencesResponse:
    # Ownership first (404 "Repository not found"), then the per-user limit (429).
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    await limiter.hit(OCCURRENCES_LIMIT, str(user.id))
    try:
        return await code_occurrences(db, repository, path, line, column)
    except FileContentError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
