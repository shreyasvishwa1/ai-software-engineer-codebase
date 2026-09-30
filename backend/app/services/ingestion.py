"""Synchronous ZIP ingestion for a repository (uploaded ZIP or public GitHub archive).

Each run is recorded as an ingestion AnalysisJob (pending -> processing -> completed/failed)
that is committed together with the repository claim and with the final outcome.

Lifecycle (Repository.status): claim ``processing`` -> obtain the archive (the upload, or a
GitHub download) -> extract into a staging directory ->
replace the repository's File rows and set ``ready`` in one transaction -> swap the staging
directory into DATA_DIR/repositories/<repository_id>/. Any failure rolls the database
work back, marks the repository ``failed`` and removes the staging directory, so the
previous File rows and stored files are preserved.
"""

import os
import shutil
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import IO

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.models import File, Repository, RepositoryStatus
from app.services.analysis_jobs import (
    mark_completed,
    mark_failed,
    mark_processing,
    new_ingestion_job,
)
from app.services.archive import MIB, ArchiveError, ArchiveLimits, extract_zip
from app.services.github import (
    GitHubError,
    archive_download_url,
    download_archive,
    parse_github_repository_url,
)

# Returns the archive to ingest; called after the repository has been claimed.
ArchiveProvider = Callable[[], Awaitable[IO[bytes]]]

# Stored on the job for unexpected (non-controlled) failures; never exception text.
INTERNAL_FAILURE_MESSAGE = "Ingestion failed due to an internal error"


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


async def ingest_zip_archive(
    db: AsyncSession, repository: Repository, archive: IO[bytes]
) -> int:
    """Ingest an uploaded ZIP ``archive`` and return the number of registered files."""

    async def provide_upload() -> IO[bytes]:
        return archive

    return await _ingest(db, repository, provide_upload)


async def ingest_github_repository(db: AsyncSession, repository: Repository) -> int:
    """Download the public GitHub archive for ``repository.source_url`` and ingest it.

    The URL and branch are validated before the repository is claimed, so invalid sources
    leave the status unchanged. The download goes to an anonymous temporary file that is
    deleted when closed, and then through exactly the same pipeline as an uploaded ZIP.
    """
    try:
        source = parse_github_repository_url(repository.source_url)
        url = archive_download_url(source, repository.default_branch)
    except GitHubError as exc:
        raise IngestionError(exc.status_code, exc.detail) from None
    max_bytes = get_settings().max_upload_size_mb * MIB

    with tempfile.TemporaryFile() as download:

        async def provide_download() -> IO[bytes]:
            await download_archive(url, download, max_bytes)
            return download

        return await _ingest(db, repository, provide_download)


async def _ingest(
    db: AsyncSession, repository: Repository, provide_archive: ArchiveProvider
) -> int:
    """Run the ingestion lifecycle on the archive returned by ``provide_archive``."""
    settings = get_settings()
    repository_id = repository.id

    # Atomic claim plus job creation in one transaction: either both are committed or
    # neither is, so a failed job insert never leaves the repository stuck in processing,
    # and a rejected claim never leaves a job behind.
    already_running = IngestionError(409, "Repository ingestion is already in progress")
    try:
        claimed = await db.execute(
            update(Repository)
            .where(
                Repository.id == repository_id, Repository.status != RepositoryStatus.PROCESSING
            )
            .values(status=RepositoryStatus.PROCESSING)
            .returning(Repository.id)
        )
        if claimed.scalar_one_or_none() is None:
            raise already_running
        job = new_ingestion_job(repository)  # pending
        db.add(job)
        await db.flush()
        job_id = job.id
        await mark_processing(db, job_id)
        await db.commit()
    except IntegrityError:
        # The active-ingestion unique index rejected a second active job.
        await db.rollback()
        raise already_running from None
    except Exception:
        await db.rollback()
        raise

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
    return len(files)
