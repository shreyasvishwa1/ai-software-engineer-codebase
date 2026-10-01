import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.api.v1.routes.repositories import _get_owned_repository
from app.database import get_db
from app.models import AnalysisJobStatus, User
from app.schemas.code_metrics import CodeMetricsJobResponse, CodeMetricsResponse
from app.services.code_metrics import (
    CodeMetricsError,
    code_metrics_results,
    start_code_metrics_job,
)

# Code-metrics analysis of a repository's current snapshot; every route requires an
# authenticated user and goes through the existing ownership lookup.
router = APIRouter(
    prefix="/projects/{project_id}/repositories/{repository_id}/analysis/code-metrics",
    tags=["code analysis"],
    dependencies=[Depends(get_current_user)],
)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post("", response_model=CodeMetricsJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_code_metrics(
    project_id: uuid.UUID, repository_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> CodeMetricsJobResponse:
    # 404 "Repository not found" for missing, wrong-project and other users' repositories.
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    try:
        job_id = await start_code_metrics_job(db, repository)
    except CodeMetricsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    return CodeMetricsJobResponse(
        job_id=job_id, repository_id=repository_id, status=AnalysisJobStatus.PENDING
    )


@router.get("", response_model=CodeMetricsResponse)
async def get_code_metrics(
    project_id: uuid.UUID, repository_id: uuid.UUID, db: DbSession, user: CurrentUser
) -> CodeMetricsResponse:
    repository = await _get_owned_repository(db, project_id, repository_id, user)
    return await code_metrics_results(db, repository)
