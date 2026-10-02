import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.anthropic_provider import get_llm_provider
from app.ai.provider import LLMProvider
from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.file_explanation import FileExplanationResponse
from app.services.file_content import FileContentError
from app.services.file_explanation import (
    EXPLAIN_LIMIT,
    NOT_CONFIGURED,
    FileExplanationError,
    explain_file,
)
from app.services.rate_limit import RateLimit

# AI explanation of one repository file; requires an authenticated user and goes through
# the existing ownership lookup. All generation settings are server-side.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
Provider = Annotated[LLMProvider | None, Depends(get_llm_provider)]


@router.post("/files/explain", response_model=FileExplanationResponse)
async def explain_repository_file(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    path: Annotated[str, Query(min_length=1, max_length=4096)],
    db: DbSession,
    user: CurrentUser,
    limiter: RateLimit,
    provider: Provider,
) -> FileExplanationResponse:
    # Ownership first (404 "Repository not found"), then configuration, then the limit.
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    if provider is None:
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
    await limiter.hit(EXPLAIN_LIMIT, str(user.id))
    try:
        return await explain_file(db, repository, path, provider)
    except (FileContentError, FileExplanationError) as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
