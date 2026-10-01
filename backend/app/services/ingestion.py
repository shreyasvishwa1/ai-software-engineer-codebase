"""Repository ingestion (uploaded ZIP or public GitHub archive), run by the arq worker.

API side: ``create_ingestion_job`` records a pending AnalysisJob (and, for ZIP uploads,
stores the archive under DATA_DIR/ingestion-uploads/<job_id>.zip), then
``queue_ingestion_job`` enqueues only the job id.

Worker side: ``run_ingestion_job`` loads and re-validates the job, atomically claims it
(pending -> processing) together with the repository, and runs the pipeline; the job ends
completed or failed, and the uploaded archive is always removed.

Pipeline (Repository.status): ``processing`` -> obtain the archive (the stored upload, or a
GitHub download) -> extract into a staging directory ->
replace the repository's File rows and set ``ready`` in one transaction -> swap the staging
directory into DATA_DIR/repositories/<repository_id>/. Any failure rolls the database
work back, marks the repository ``failed`` and removes the staging directory, so the
previous File rows and stored files are preserved.
"""

import contextlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.models import (
    AnalysisJob,
    AnalysisJobStatus,
    AnalysisJobType,
    File,
    Repository,
    RepositorySourceType,
    RepositoryStatus,
)
from app.services.analysis_jobs import (
    claim_pending_job,
    fail_pending_job,
    mark_completed,
    mark_failed,
    new_ingestion_job,
)
from app.services.archive import MIB, ArchiveError, ArchiveLimits, extract_zip
from app.services.github import (
    GitHubError,
    archive_download_url,
    download_archive,
    parse_github_repository_url,
)
from app.services.languages import detect_language
from app.workers import queue

# Returns the archive to ingest; called after the repository has been claimed.
ArchiveProvider = Callable[[], Awaitable[IO[bytes]]]

# Stored on the job for unexpected (non-controlled) failures; never exception text.
INTERNAL_FAILURE_MESSAGE = "Ingestion failed due to an internal error"
QUEUE_FAILURE_MESSAGE = "Ingestion could not be queued; try again later"
INVALID_JOB_MESSAGE = "Invalid ingestion job"
_COPY_CHUNK_SIZE = 1024 * 1024


class IngestionError(Exception):
    """A controlled ingestion failure, safe to return to the client."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def archive_limits(settings: Settings) -> ArchiveLimits:
    return ArchiveLimits(
        max_entries=settings.max_archive_file_count,
        max_total_bytes=settings.max_extracted_size_mb * MIB,
        max_file_bytes=settings.max_extracted_file_size_mb * MIB,
        max_compression_ratio=settings.max_compression_ratio,
    )


def repositories_root(settings: Settings) -> Path:
    return settings.data_dir / "repositories"


def repository_storage_dir(settings: Settings, repository_id: uuid.UUID) -> Path:
    return repositories_root(settings) / str(repository_id)


def _new_staging_dir(settings: Settings, repository_id: uuid.UUID) -> Path:
    root = repositories_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    # Same parent as the final directory, so the swap is a rename on one filesystem.
    staging = root / f".staging-{repository_id}-{uuid.uuid4().hex}"
    staging.mkdir()
    return staging


def swap_into_place(staging: Path, final: Path) -> None:
    """Replace ``final`` with ``staging``, restoring the previous directory on failure."""
    previous: Path | None = None
    if final.exists():
        previous = final.with_name(f".old-{final.name}-{uuid.uuid4().hex}")
        os.replace(final, previous)
    try:
        os.replace(staging, final)
    except OSError:
        if previous is not None:
            os.replace(previous, final)
        raise
    if previous is not None:
        shutil.rmtree(previous, ignore_errors=True)


def _remove_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


async def _mark_failed(
    db: AsyncSession, repository_id: uuid.UUID, job_id: uuid.UUID, error_message: str
) -> None:
    """Mark the repository and its ingestion job failed together."""
    await db.execute(
        update(Repository)
        .where(Repository.id == repository_id)
        .values(status=RepositoryStatus.FAILED)
    )
    await mark_failed(db, job_id, error_message)
    await db.commit()


def ingestion_uploads_root(settings: Settings) -> Path:
    return settings.data_dir / "ingestion-uploads"


def ingestion_artifact_path(settings: Settings, job_id: uuid.UUID) -> Path:
    """Where an uploaded ZIP waits for its job: named only by the (random) job UUID."""
    return ingestion_uploads_root(settings) / f"{job_id}.zip"


def _save_artifact(source: IO[bytes], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    source.seek(0)
    with open(dest, "xb") as out:  # never overwrites
        shutil.copyfileobj(source, out, _COPY_CHUNK_SIZE)


def _remove_file(path: Path) -> None:
    path.unlink(missing_ok=True)


def validate_github_source(repository: Repository) -> str:
    """Validate the repository's GitHub URL/branch (no network) and return the archive URL."""
    try:
        source = parse_github_repository_url(repository.source_url)
        return archive_download_url(source, repository.default_branch)
    except GitHubError as exc:
        raise IngestionError(exc.status_code, exc.detail) from None


# --- API side: record the job and queue it -----------------------------------------------


async def create_ingestion_job(
    db: AsyncSession, repository: Repository, upload: IO[bytes] | None = None
) -> uuid.UUID:
    """Create a pending ingestion job for an authorized repository and commit it.

    For ZIP repositories ``upload`` is copied to DATA_DIR/ingestion-uploads/<job_id>.zip so
    it outlives the request. Nothing is extracted here. A repository that is processing,
    or that already has an active ingestion job (unique index), is rejected with 409.
    """
    already_running = IngestionError(409, "Repository ingestion is already in progress")
    if repository.status == RepositoryStatus.PROCESSING:
        raise already_running
    settings = get_settings()
    job = new_ingestion_job(repository)  # pending, progress 0
    artifact: Path | None = None
    try:
        db.add(job)
        await db.flush()
        if upload is not None:
            artifact = ingestion_artifact_path(settings, job.id)
            await run_in_threadpool(_save_artifact, upload, artifact)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise already_running from None
    except Exception:
        await db.rollback()
        if artifact is not None:
            await run_in_threadpool(_remove_file, artifact)
        raise
    return job.id


async def queue_ingestion_job(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Enqueue ``job_id``. If Redis is unavailable, the job is failed and its upload removed."""
    try:
        await queue.enqueue_ingestion_job(job_id)
    except Exception:
        await db.rollback()
        if await fail_pending_job(db, job_id, QUEUE_FAILURE_MESSAGE):
            await db.commit()
            await run_in_threadpool(_remove_file, ingestion_artifact_path(get_settings(), job_id))
        raise IngestionError(503, QUEUE_FAILURE_MESSAGE) from None


# --- Worker side: claim and run the job --------------------------------------------------


async def run_ingestion_job(db: AsyncSession, job_id: uuid.UUID) -> str:
    """Execute one queued ingestion job and return its outcome.

    Everything is loaded from PostgreSQL and re-validated; the queue payload is only the id.
    The job is claimed atomically (pending -> processing), so a duplicate delivery or a
    second worker does nothing. Failures are recorded on the job and never raised.
    """
    settings = get_settings()
    artifact = ingestion_artifact_path(settings, job_id)

    job = await db.get(AnalysisJob, job_id)
    if job is None:  # e.g. deleted with its repository before it ran
        await run_in_threadpool(_remove_file, artifact)
        return "missing"
    repository = None
    if job.job_type == AnalysisJobType.INGESTION and job.repository_id is not None:
        repository = await db.scalar(
            select(Repository).where(
                Repository.id == job.repository_id, Repository.project_id == job.project_id
            )
        )
    if repository is None:
        await fail_pending_job(db, job_id, INVALID_JOB_MESSAGE)
        await db.commit()
        await run_in_threadpool(_remove_file, artifact)
        return "invalid"
    repository_id = repository.id

    # Claim the job and the repository in one transaction.
    if not await claim_pending_job(db, job_id):
        await db.rollback()
        # Another worker owns it or it already finished; only clean up a finished job.
        current = await db.scalar(select(AnalysisJob.status).where(AnalysisJob.id == job_id))
        if current in (AnalysisJobStatus.COMPLETED, AnalysisJobStatus.FAILED):
            await run_in_threadpool(_remove_file, artifact)
        return "skipped"
    repository_claimed = await db.execute(
        update(Repository)
        .where(Repository.id == repository_id, Repository.status != RepositoryStatus.PROCESSING)
        .values(status=RepositoryStatus.PROCESSING)
        .returning(Repository.id)
    )
    if repository_claimed.scalar_one_or_none() is None:
        await db.rollback()
        await fail_pending_job(db, job_id, "Repository ingestion is already in progress")
        await db.commit()
        await run_in_threadpool(_remove_file, artifact)
        return "failed"
    await db.commit()

    try:
        if repository.source_type == RepositorySourceType.GITHUB:
            await _ingest_github(db, repository, job_id)
        else:
            await _ingest_upload(db, repository, job_id, artifact)
    except Exception:
        return "failed"  # already recorded on the job and the repository
    finally:
        await run_in_threadpool(_remove_file, artifact)
    return "completed"


async def _ingest_upload(
    db: AsyncSession, repository: Repository, job_id: uuid.UUID, artifact: Path
) -> None:
    with contextlib.ExitStack() as stack:

        async def provide_upload() -> IO[bytes]:
            try:
                return stack.enter_context(open(artifact, "rb"))
            except FileNotFoundError:
                raise IngestionError(500, "Uploaded archive is no longer available") from None

        await _run_pipeline(db, repository, job_id, provide_upload)


async def _ingest_github(db: AsyncSession, repository: Repository, job_id: uuid.UUID) -> None:
    max_bytes = get_settings().max_upload_size_mb * MIB
    # The download goes to an anonymous temporary file that is deleted when closed.
    with tempfile.TemporaryFile() as download:

        async def provide_download() -> IO[bytes]:
            url = validate_github_source(repository)  # re-validated in the worker
            await download_archive(url, download, max_bytes)
            return download

        await _run_pipeline(db, repository, job_id, provide_download)


async def _run_pipeline(
    db: AsyncSession, repository: Repository, job_id: uuid.UUID, provide_archive: ArchiveProvider
) -> None:
    """Extract, register files and swap storage for a claimed job; on failure, record it on
    the job and the repository and re-raise."""
    settings = get_settings()
    repository_id = repository.id
    staging: Path | None = None
    try:
        try:
            archive = await provide_archive()
            staging = await run_in_threadpool(_new_staging_dir, settings, repository_id)
            files = await run_in_threadpool(extract_zip, archive, staging, archive_limits(settings))
        except (ArchiveError, GitHubError) as exc:
            raise IngestionError(exc.status_code, exc.detail) from None

        await db.execute(delete(File).where(File.repository_id == repository_id))
        db.add_all(
            File(
                repository_id=repository_id,
                path=f.path,
                name=f.name,
                extension=f.extension,
                language=detect_language(f.name, f.extension),
                size_bytes=f.size_bytes,
                sha256=f.sha256,
            )
            for f in files
        )
        await db.execute(
            update(Repository)
            .where(Repository.id == repository_id)
            .values(status=RepositoryStatus.READY)
        )
        await mark_completed(db, job_id)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        if staging is not None:
            await run_in_threadpool(_remove_dir, staging)
        message = exc.detail if isinstance(exc, IngestionError) else INTERNAL_FAILURE_MESSAGE
        await _mark_failed(db, repository_id, job_id, message)
        raise

    try:
        await run_in_threadpool(
            swap_into_place, staging, repository_storage_dir(settings, repository_id)
        )
    except OSError:
        await run_in_threadpool(_remove_dir, staging)
        storage_error = IngestionError(500, "Repository storage could not be updated")
        await _mark_failed(db, repository_id, job_id, storage_error.detail)
        raise storage_error from None
