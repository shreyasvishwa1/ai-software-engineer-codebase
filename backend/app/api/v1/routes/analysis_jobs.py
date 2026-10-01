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
from app.services.job_cancellation import cancel_pending_ingestion_job

# Job inspection, plus cancelling a job that has not started yet; every route requires an
# authenticated user.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}/jobs",
    tags=["analysis jobs"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

JOB_NOT_FOUND = "Analysis job not found"
NOT_CANCELLABLE = "Only pending jobs can be cancelled"


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


@router.post("/{job_id}/cancel", response_model=AnalysisJobResponse)
async def cancel_job(
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    job_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
) -> AnalysisJob:
    """Cancel a pending ingestion or analysis job (it becomes failed with "Ingestion was
    cancelled" or "Analysis was cancelled"). Jobs that are processing or finished are left
    unchanged (409)."""
    owned = (
        _owned_jobs(project_id, repository_id, user)
        .where(AnalysisJob.id == job_id)
        .execution_options(populate_existing=True)  # always the current row
    )
    if await db.scalar(owned) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOB_NOT_FOUND)
    cancelled = await cancel_pending_ingestion_job(db, job_id)
    job = await db.scalar(owned)
    if job is None:  # deleted (e.g. with its repository) in the meantime
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=JOB_NOT_FOUND)
    if not cancelled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_CANCELLABLE)
    return job
