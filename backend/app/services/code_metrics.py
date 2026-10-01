"""Code-metrics analysis: per-file line counts of a repository's current snapshot.

API side: ``start_code_metrics_job`` records a pending ``code_metrics`` AnalysisJob for a
ready repository (first recovering a stale one) and queues it; ``code_metrics_results``
returns the stored metrics of the current snapshot.

Worker side: ``run_code_metrics_job`` claims the job (pending -> processing, the shared
conditional claim), captures the snapshot (the repository's files rows), measures each
file and stores the results. Files are read only through the Step 21 verified reader
(links refused, containment checked, size bounded, SHA-256 verified) and decoded with its
strict UTF-8 decoder; nothing is logged and no content is stored.

Snapshot consistency: re-ingestion replaces every files row (new ids) in one transaction
that deletes the old rows before anything else, and is never blocked by analysis. Before
storing results the analysis takes FOR KEY SHARE NOWAIT locks on the repository's current
files rows and compares their ids with the captured snapshot. If a re-ingestion holds the
rows (NOWAIT fails at once, so the analysis never waits on it) or has already replaced
them, the job fails with CHANGED_MESSAGE. Otherwise the held locks keep the snapshot
current until the results are committed; a later re-ingestion removes them with the files
rows (ON DELETE CASCADE). Metrics are therefore only ever shown for the snapshot they were
computed from.
"""

import uuid
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import (
    AnalysisJob,
    AnalysisJobStatus,
    AnalysisJobType,
    File,
    FileMetric,
    Repository,
    RepositoryStatus,
)
from app.schemas.analysis_job import AnalysisJobResponse
from app.schemas.code_metrics import CodeMetricsResponse, CodeMetricsSummary, FileMetricResponse
from app.services.analysis_jobs import claim_pending_job, fail_pending_job

# Step 21's verified reader and decoder, reused unchanged (never duplicated).
from app.services.file_content import MAX_CONTENT_BYTES, _decode, _read_verified, _Unavailable
from app.services.ingestion import repository_storage_dir
from app.workers import queue

# Code-metrics stale-job thresholds (the Step 16 maintenance task recovers ingestion jobs
# only). Same values as the ingestion thresholds: the worker's job timeout (1800 s) plus
# grace, and 6 hours for a job that was never started.
CODE_METRICS_PROCESSING_STALE_AFTER = timedelta(seconds=1800 + 300)
CODE_METRICS_PENDING_STALE_AFTER = timedelta(hours=6)

NOT_READY = "Repository must be ingested before analysis"
INGESTION_IN_PROGRESS = "Repository ingestion is in progress"
ALREADY_RUNNING = "Code analysis is already in progress"
INVALID_JOB_MESSAGE = "Invalid analysis job"
QUEUE_FAILURE_MESSAGE = "Analysis could not be queued; try again later"
CHANGED_MESSAGE = "Repository changed during analysis; run it again"
INTERNAL_FAILURE_MESSAGE = "Analysis failed due to an internal error"
STALE_PROCESSING_MESSAGE = "Analysis was interrupted; please run it again"
STALE_PENDING_MESSAGE = "Analysis was not started; please run it again"

_ACTIVE = (AnalysisJobStatus.PENDING, AnalysisJobStatus.PROCESSING)
_LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL SQLSTATE raised by NOWAIT

Measurement = tuple[str, int | None, int | None]  # (status, line_count, blank_line_count)


class CodeMetricsError(Exception):
    """A controlled failure, safe to return to the client."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --- Measuring -----------------------------------------------------------------------------


def count_lines(text: str) -> tuple[int, int]:
    """(lines, blank lines). CRLF and CR count as line breaks; a final line without a
    trailing newline counts; an empty text has no lines; blank means whitespace only."""
    if not text:
        return 0, 0
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if normalized.endswith("\n"):
        lines.pop()
    return len(lines), sum(1 for line in lines if not line.strip())


def _measure(root: Path, path: str, size_bytes: int, sha256: str | None) -> Measurement:
    if size_bytes > MAX_CONTENT_BYTES:
        return "too_large", None, None  # never opened
    try:
        data = _read_verified(root, path, sha256)
    except _Unavailable:
        return "unavailable", None, None
    text = _decode(data)
    if text is None:
        return "binary", None, None
    lines, blank = count_lines(text)
    return "analyzed", lines, blank


# --- API side ------------------------------------------------------------------------------


async def recover_stale_code_metrics_jobs(db: AsyncSession, repository_id: uuid.UUID) -> int:
    """Fail this repository's stale active code-metrics jobs and commit. Conditional: only
    rows still pending/processing past the thresholds change, so a job claimed, finished or
    cancelled in the meantime is never overwritten. Other job types are never touched."""
    now = func.now()
    stale_processing = await db.execute(
        update(AnalysisJob)
        .where(
            AnalysisJob.repository_id == repository_id,
            AnalysisJob.job_type == AnalysisJobType.CODE_METRICS,
            AnalysisJob.status == AnalysisJobStatus.PROCESSING,
            or_(
                AnalysisJob.started_at < now - CODE_METRICS_PROCESSING_STALE_AFTER,
                and_(
                    AnalysisJob.started_at.is_(None),
                    AnalysisJob.created_at < now - CODE_METRICS_PROCESSING_STALE_AFTER,
                ),
            ),
        )
        .values(
            status=AnalysisJobStatus.FAILED,
            error_message=STALE_PROCESSING_MESSAGE,
            completed_at=now,
        )
        .returning(AnalysisJob.id)
        .execution_options(synchronize_session=False)
    )
    stale_pending = await db.execute(
        update(AnalysisJob)
        .where(
            AnalysisJob.repository_id == repository_id,
            AnalysisJob.job_type == AnalysisJobType.CODE_METRICS,
            AnalysisJob.status == AnalysisJobStatus.PENDING,
            AnalysisJob.created_at < now - CODE_METRICS_PENDING_STALE_AFTER,
        )
        .values(
            status=AnalysisJobStatus.FAILED,
            error_message=STALE_PENDING_MESSAGE,
            completed_at=now,
        )
        .returning(AnalysisJob.id)
        .execution_options(synchronize_session=False)
    )
    recovered = len(stale_processing.all()) + len(stale_pending.all())
    await db.commit()
    return recovered


async def start_code_metrics_job(db: AsyncSession, repository: Repository) -> uuid.UUID:
    """Create and queue a code-metrics job for an authorized repository; return its id."""
    repository_id, project_id = repository.id, repository.project_id
    if repository.status == RepositoryStatus.PROCESSING:
        raise CodeMetricsError(409, INGESTION_IN_PROGRESS)
    if repository.status != RepositoryStatus.READY:
        raise CodeMetricsError(409, NOT_READY)
    active_ingestion = await db.scalar(
        select(AnalysisJob.id)
        .where(
            AnalysisJob.repository_id == repository_id,
            AnalysisJob.job_type == AnalysisJobType.INGESTION,
            AnalysisJob.status.in_(_ACTIVE),
        )
        .limit(1)
    )
    if active_ingestion is not None:
        raise CodeMetricsError(409, INGESTION_IN_PROGRESS)

    await recover_stale_code_metrics_jobs(db, repository_id)
    job = AnalysisJob(
        project_id=project_id,
        repository_id=repository_id,
        job_type=AnalysisJobType.CODE_METRICS,
        status=AnalysisJobStatus.PENDING,
        progress=0,
    )
    db.add(job)
    try:
        await db.commit()
    except IntegrityError:  # another active code-metrics job (partial unique index)
        await db.rollback()
        raise CodeMetricsError(409, ALREADY_RUNNING) from None
    job_id = job.id

    try:
        await queue.enqueue_code_metrics_job(job_id)
    except Exception:
        await db.rollback()
        if await fail_pending_job(db, job_id, QUEUE_FAILURE_MESSAGE):
            await db.commit()
        raise CodeMetricsError(503, QUEUE_FAILURE_MESSAGE) from None
    return job_id


async def code_metrics_results(db: AsyncSession, repository: Repository) -> CodeMetricsResponse:
    """The latest code-metrics job and the metrics of the repository's current snapshot."""
    job = await db.scalar(
        select(AnalysisJob)
        .where(
            AnalysisJob.repository_id == repository.id,
            AnalysisJob.job_type == AnalysisJobType.CODE_METRICS,
        )
        .order_by(AnalysisJob.created_at.desc(), AnalysisJob.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)  # always the current row
    )
    rows = (
        await db.execute(
            select(
                File.path,
                File.language,
                FileMetric.status,
                FileMetric.line_count,
                FileMetric.blank_line_count,
            )
            .join(FileMetric, FileMetric.file_id == File.id, isouter=True)
            .where(File.repository_id == repository.id)
        )
    ).all()
    job_response = AnalysisJobResponse.model_validate(job) if job is not None else None
    # Metrics are stored for a whole snapshot at once, so they exist for every current file
    # or for none (after a re-ingestion, until analyzed again).
    analyzed = all(row.status is not None for row in rows) and (
        bool(rows) or (job is not None and job.status == AnalysisJobStatus.COMPLETED)
    )
    if not analyzed:
        return CodeMetricsResponse(job=job_response, summary=None, files=[])

    files = [
        FileMetricResponse.model_validate(
            {
                "path": row.path,
                "language": row.language,
                "status": row.status,
                "line_count": row.line_count,
                "blank_line_count": row.blank_line_count,
            }
        )
        for row in sorted(rows, key=lambda r: r.path)
    ]
    counts = {status: 0 for status in ("analyzed", "binary", "too_large", "unavailable")}
    for item in files:
        counts[item.status] += 1
    summary = CodeMetricsSummary(
        files=len(files),
        analyzed=counts["analyzed"],
        binary=counts["binary"],
        too_large=counts["too_large"],
        unavailable=counts["unavailable"],
        total_lines=sum(item.line_count or 0 for item in files),
        blank_lines=sum(item.blank_line_count or 0 for item in files),
    )
    return CodeMetricsResponse(job=job_response, summary=summary, files=files)


# --- Worker side ---------------------------------------------------------------------------


async def _fail_processing(db: AsyncSession, job_id: uuid.UUID, message: str) -> None:
    """Fail the job only if it is still processing (never overwrites a recovered job)."""
    await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id, AnalysisJob.status == AnalysisJobStatus.PROCESSING)
        .values(status=AnalysisJobStatus.FAILED, error_message=message, completed_at=func.now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def _lock_current_snapshot(
    db: AsyncSession, repository_id: uuid.UUID
) -> set[uuid.UUID] | None:
    """Ids of the repository's current files rows, locked FOR KEY SHARE NOWAIT until the
    transaction ends; None if a re-ingestion holds them."""
    try:
        result = await db.scalars(
            select(File.id)
            .where(File.repository_id == repository_id)
            .with_for_update(key_share=True, nowait=True)
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
            return None
        raise
    return set(result.all())


async def _store_results(
    db: AsyncSession,
    job_id: uuid.UUID,
    repository_id: uuid.UUID,
    snapshot: set[uuid.UUID],
    results: Sequence[tuple[uuid.UUID, Measurement]],
) -> str:
    current = await _lock_current_snapshot(db, repository_id)
    if current != snapshot:
        await db.rollback()
        await _fail_processing(db, job_id, CHANGED_MESSAGE)
        return "failed"
    completed = await db.execute(
        update(AnalysisJob)
        .where(AnalysisJob.id == job_id, AnalysisJob.status == AnalysisJobStatus.PROCESSING)
        .values(status=AnalysisJobStatus.COMPLETED, progress=100, completed_at=func.now())
        .returning(AnalysisJob.id)
        .execution_options(synchronize_session=False)
    )
    if completed.scalar_one_or_none() is None:  # recovered as stale meanwhile
        await db.rollback()
        return "skipped"
    await db.execute(delete(FileMetric).where(FileMetric.repository_id == repository_id))
    rows: list[dict[str, Any]] = [
        {
            "file_id": file_id,
            "repository_id": repository_id,
            "analysis_job_id": job_id,
            "status": status,
            "line_count": lines,
            "blank_line_count": blank,
        }
        for file_id, (status, lines, blank) in results
    ]
    if rows:
        await db.execute(insert(FileMetric), rows)
    await db.commit()
    return "completed"


async def run_code_metrics_job(db: AsyncSession, job_id: uuid.UUID) -> str:
    """Execute one queued code-metrics job and return its outcome. Failures are recorded
    on the job and never raised. Jobs of other types are never touched."""
    job = await db.get(AnalysisJob, job_id)
    if job is None:
        return "missing"
    if job.job_type != AnalysisJobType.CODE_METRICS:
        return "invalid"
    repository = await db.scalar(
        select(Repository).where(
            Repository.id == job.repository_id, Repository.project_id == job.project_id
        )
    )
    if repository is None:
        await fail_pending_job(db, job_id, INVALID_JOB_MESSAGE)
        await db.commit()
        return "invalid"
    repository_id = repository.id

    if not await claim_pending_job(db, job_id):
        await db.rollback()  # claimed elsewhere, finished, cancelled or recovered
        return "skipped"
    await db.commit()

    try:
        snapshot_rows = (
            await db.execute(
                select(File.id, File.path, File.size_bytes, File.sha256).where(
                    File.repository_id == repository_id
                )
            )
        ).all()
        await db.commit()  # no transaction is held while files are read
        root = repository_storage_dir(get_settings(), repository_id)
        results = [
            (
                row.id,
                await run_in_threadpool(_measure, root, row.path, row.size_bytes, row.sha256),
            )
            for row in snapshot_rows
        ]
        return await _store_results(
            db, job_id, repository_id, {row.id for row in snapshot_rows}, results
        )
    except Exception:
        await db.rollback()
        await _fail_processing(db, job_id, INTERNAL_FAILURE_MESSAGE)
        return "failed"
