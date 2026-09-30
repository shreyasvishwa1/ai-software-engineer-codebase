"""Internal lifecycle helpers for repository analysis jobs.

Jobs are created only by application services for an already authorized repository.
Lifecycle: pending -> processing -> completed | failed. Progress is 0 until completion
(100): the synchronous pipeline has no meaningful intermediate percentages.
"""

import uuid

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AnalysisJob, AnalysisJobStatus, AnalysisJobType, Repository


def new_ingestion_job(repository: Repository) -> AnalysisJob:
    """A pending ingestion job; all associations come from the authorized repository."""
    return AnalysisJob(
        project_id=repository.project_id,
        repository_id=repository.id,
        job_type=AnalysisJobType.INGESTION,
        status=AnalysisJobStatus.PENDING,
        progress=0,
    )


async def claim_pending_job(db: AsyncSession, job_id: uuid.UUID) -> bool:
    """Atomically move a pending job to processing. Only one caller can succeed: the
    conditional UPDATE matches no row once another worker has claimed (or finished) it."""
    result = await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id, AnalysisJob.status == AnalysisJobStatus.PENDING)
        .values(status=AnalysisJobStatus.PROCESSING, started_at=func.now())
        .returning(AnalysisJob.id)
    )
    return result.scalar_one_or_none() is not None


async def fail_pending_job(db: AsyncSession, job_id: uuid.UUID, error_message: str) -> bool:
    """Fail a job that never started (e.g. it could not be queued). No-op otherwise."""
    result = await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id, AnalysisJob.status == AnalysisJobStatus.PENDING)
        .values(
            status=AnalysisJobStatus.FAILED,
            error_message=error_message,
            completed_at=func.now(),
        )
        .returning(AnalysisJob.id)
    )
    return result.scalar_one_or_none() is not None


async def mark_completed(db: AsyncSession, job_id: uuid.UUID) -> None:
    await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id)
        .values(status=AnalysisJobStatus.COMPLETED, progress=100, completed_at=func.now())
    )


async def mark_failed(db: AsyncSession, job_id: uuid.UUID, error_message: str) -> None:
    """``error_message`` must be a controlled, client-safe message."""
    await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id)
        .values(
            status=AnalysisJobStatus.FAILED,
            error_message=error_message,
            completed_at=func.now(),
        )
    )
