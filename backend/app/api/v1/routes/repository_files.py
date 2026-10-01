import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import User
from app.schemas.repository_files import RepositoryLanguagesResponse, RepositoryTreeResponse
from app.services.repository_files import language_statistics, repository_tree

# Read-only views of a repository's registered files; every route requires an
# authenticated user and goes through the existing ownership lookup.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}",
    tags=["repositories"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/tree", response_model=RepositoryTreeResponse)
async def get_repository_tree(
    project_id: uuid.UUID, repository_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> RepositoryTreeResponse:
    # 404 "Repository not found" for missing, wrong-project and other users' repositories.
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    return await repository_tree(db, repository)


@router.get("/languages", response_model=RepositoryLanguagesResponse)
async def get_repository_languages(
    project_id: uuid.UUID, repository_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> RepositoryLanguagesResponse:
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    return await language_statistics(db, repository)
