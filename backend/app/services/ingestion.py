"""Synchronous ZIP ingestion for a repository.

Lifecycle (Repository.status): claim ``processing`` -> extract into a staging directory ->
replace the repository's File rows and set ``ready`` in one transaction -> swap the staging
directory into DATA_DIR/repositories/<repository_id>/. Any failure rolls the database
work back, marks the repository ``failed`` and removes the staging directory, so the
previous File rows and stored files are preserved.
"""

import os
import shutil
import uuid
from pathlib import Path
from typing import IO

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.models import File, Repository, RepositoryStatus
from app.services.archive import MIB, ArchiveError, ArchiveLimits, extract_zip


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


async def _mark_failed(db: AsyncSession, repository_id: uuid.UUID) -> None:
    await db.execute(
        update(Repository)
        .where(Repository.id == repository_id)
        .values(status=RepositoryStatus.FAILED)
    )
    await db.commit()


async def ingest_zip_archive(
    db: AsyncSession, repository: Repository, archive: IO[bytes]
) -> int:
    """Ingest ``archive`` into ``repository`` and return the number of registered files."""
    settings = get_settings()
    repository_id = repository.id

    # Atomic claim: a concurrent ingestion of the same repository is rejected.
    claimed = await db.execute(
        update(Repository)
        .where(Repository.id == repository_id, Repository.status != RepositoryStatus.PROCESSING)
        .values(status=RepositoryStatus.PROCESSING)
        .returning(Repository.id)
    )
    if claimed.scalar_one_or_none() is None:
        raise IngestionError(409, "Repository ingestion is already in progress")
    await db.commit()

    staging: Path | None = None
    try:
        staging = await run_in_threadpool(_new_staging_dir, settings, repository_id)
        try:
            files = await run_in_threadpool(extract_zip, archive, staging, archive_limits(settings))
        except ArchiveError as exc:
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
        await db.commit()
    except Exception:
        await db.rollback()
        if staging is not None:
            await run_in_threadpool(_remove_dir, staging)
        await _mark_failed(db, repository_id)
        raise

    try:
        await run_in_threadpool(
            swap_into_place, staging, repository_storage_dir(settings, repository_id)
        )
    except OSError:
        await run_in_threadpool(_remove_dir, staging)
        await _mark_failed(db, repository_id)
        raise IngestionError(500, "Repository storage could not be updated") from None
    return len(files)
