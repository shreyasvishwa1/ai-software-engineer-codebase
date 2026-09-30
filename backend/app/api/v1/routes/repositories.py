import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.database import get_db
from app.models import Project, Repository, User
from app.schemas.repository import RepositoryCreate, RepositoryResponse, RepositoryUpdate

# Every route requires an authenticated user.
router = APIRouter(
    prefix="/projects/{project_id}/repositories",
    tags=["repositories"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

PROJECT_NOT_FOUND = "Project not found"
REPOSITORY_NOT_FOUND = "Repository not found"


async def _ensure_owned_project(db: AsyncSession, project_id: uuid.UUID, user: User) -> None:
    """404 unless the project exists and belongs to ``user`` (other users' look missing)."""
    owned = await db.scalar(
        select(Project.id).where(Project.id == project_id, Project.user_id == user.id)
    )
    if owned is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PROJECT_NOT_FOUND)


async def _get_owned_repository(
    db: AsyncSession, project_id: uuid.UUID, repository_id: uuid.UUID, user: User
) -> Repository:
    """Load a repository in one query constrained by repository, project and owner.

    Missing, wrong-project and other-user repositories all produce the same 404.
    """
    repository = await db.scalar(
        select(Repository)
        .join(Project, Repository.project_id == Project.id)
        .where(
            Repository.id == repository_id,
            Repository.project_id == project_id,
            Project.user_id == user.id,
        )
    )
    if repository is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=REPOSITORY_NOT_FOUND)
    return repository


@router.post("", response_model=RepositoryResponse, status_code=status.HTTP_201_CREATED)
async def create_repository(
    project_id: uuid.UUID, body: RepositoryCreate, db: DbSession, user: CurrentUser
) -> Repository:
    await _ensure_owned_project(db, project_id, user)
    repository = Repository(project_id=project_id, **body.model_dump())
    db.add(repository)
    await db.commit()
    await db.refresh(repository)
    return repository


@router.get("", response_model=list[RepositoryResponse])
async def list_repositories(
    project_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> Sequence[Repository]:
    await _ensure_owned_project(db, project_id, user)
    result = await db.scalars(
        select(Repository)
        .join(Project, Repository.project_id == Project.id)
        .where(Repository.project_id == project_id, Project.user_id == user.id)
        .order_by(Repository.created_at.desc(), Repository.id.desc())
    )
    return result.all()


@router.get("/{repository_id}", response_model=RepositoryResponse)
async def get_repository(
    project_id: uuid.UUID, repository_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> Repository:
    return await _get_owned_repository(db, project_id, repository_id, user)


@router.patch("/{repository_id}", response_model=RepositoryResponse)
async def update_repository(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    body: RepositoryUpdate,
    db: DbSession,
    user: CurrentUser,
) -> Repository:
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    changes = body.model_dump(exclude_unset=True)
    if changes:
        # Only name, source_url and default_branch exist on the schema.
        for field, value in changes.items():
            setattr(repository, field, value)
        await db.commit()
        await db.refresh(repository)
    return repository


@router.delete("/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repository(
    project_id: uuid.UUID, repository_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> Response:
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    # Files are removed by the existing ON DELETE CASCADE.
    await db.delete(repository)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
