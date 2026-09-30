import uuid
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import AnalysisJob, Project, Repository, User
from app.schemas.analysis_job import AnalysisJobResponse

# Read-only job inspection; every route requires an authenticated user.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}/jobs",
    tags=["analysis jobs"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

JOB_NOT_FOUND = "Analysis job not found"


def _owned_jobs(
    project_id: uuid.UUID, repository_id: uuid.UUID, user: User
) -> Select[tuple[AnalysisJob]]:
    """Jobs of one repository, constrained by repository, project and owner in SQL."""
    return (
        select(AnalysisJob)
        .join(Repository, AnalysisJob.repository_id == Repository.id)
        .join(Project, Repository.project_id == Project.id)
        .where(
            AnalysisJob.repository_id == repository_id,
            AnalysisJob.project_id == project_id,
            Repository.project_id == project_id,
            Project.user_id == user.id,
        )
    )


@router.get("", response_model=list[AnalysisJobResponse])
async def list_jobs(
    project_id: uuid.UUID, repository_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> Sequence[AnalysisJob]:
    # 404 "Repository not found" unless the repository is the user's (as in Step 9).
    await _get_owned_repository(db, project_id, repository_id, user)
    result = await db.scalars(
        _owned_jobs(project_id, repository_id, user).order_by(
            AnalysisJob.created_at.desc(), AnalysisJob.id.desc()
        )
    )
    return result.all()


@router.get("/{job_id}", response_model=AnalysisJobResponse)
async def get_job(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    job_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> AnalysisJob:
    # One query: missing, other-repository, other-project and other-user jobs all look
    # the same.
    job = await db.scalar(
        _owned_jobs(project_id, repository_id, user).where(AnalysisJob.id == job_id)
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOB_NOT_FOUND)
    return job
