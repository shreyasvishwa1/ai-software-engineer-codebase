import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.database import get_db
from app.models import Project, User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate

# Every route requires an authenticated user.
router = APIRouter(
    prefix="/projects", tags=["projects"], dependencies=[Depends(get_current_user)]
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

PROJECT_NOT_FOUND = "Project not found"


async def _get_owned_project(db: AsyncSession, project_id: uuid.UUID, user: User) -> Project:
    """Load a project owned by ``user``; other users' projects look exactly like missing ones."""
    project = await db.scalar(
        select(Project).where(Project.id == project_id, Project.user_id == user.id)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PROJECT_NOT_FOUND)
    return project


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, db: DbSession, user: CurrentUser) -> Project:
    project = Project(user_id=user.id, name=body.name, description=body.description)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@router.get("", response_model=list[ProjectResponse])
async def list_projects(db: DbSession, user: CurrentUser) -> Sequence[Project]:
    result = await db.scalars(
        select(Project)
        .where(Project.user_id == user.id)
        .order_by(Project.created_at.desc(), Project.id.desc())
    )
    return result.all()


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Project:
    return await _get_owned_project(db, project_id, user)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: uuid.UUID, body: ProjectUpdate, db: DbSession, user: CurrentUser
) -> Project:
    project = await _get_owned_project(db, project_id, user)
    changes = body.model_dump(exclude_unset=True)
    if changes:
        for field, value in changes.items():  # only "name" and "description" exist on the schema
            setattr(project, field, value)
        await db.commit()
        await db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Response:
    project = await _get_owned_project(db, project_id, user)
    # Repositories, files and analysis jobs are removed by the existing ON DELETE CASCADE.
    await db.delete(project)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
