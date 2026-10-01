"""Scheduled ingestion maintenance, run by the arq worker (every 5 minutes and at startup).

1. Ingestion jobs stuck in ``processing`` beyond the worker's job timeout (plus grace) are
   failed, and their repository leaves ``processing``. arq has cancelled such a job already,
   so no live worker can still be running it; the previous file snapshot is untouched.
2. Ingestion jobs left ``pending`` for hours (e.g. a lost queue entry) are failed; a late
   delivery is skipped by the worker's pending-only claim.
3. Leftover filesystem state is removed: uploads of jobs that are no longer active,
   ``.staging-*`` / ``.old-*`` directories of repositories without an active job, and the
   storage directory of repositories that no longer exist. An ``.old-*`` directory is only
   removed once its repository's final directory exists again (or the repository is gone):
   while the final directory is missing, the ``.old-*`` directory is the previous snapshot
   that an in-progress swap may still restore (see ``_old_dir_is_garbage``).

Database changes use conditional updates (only rows still in the stale state change).
Filesystem deletion is restricted to exact UUID-named direct children of
DATA_DIR/repositories and DATA_DIR/ingestion-uploads; links (symlinks, Windows junctions)
are never followed: only the link entry itself is removed. Nothing is logged.
"""

import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.models import AnalysisJob, AnalysisJobStatus, AnalysisJobType, Repository, RepositoryStatus
from app.services.ingestion import ingestion_uploads_root, repositories_root

# Worker job_timeout (JOB_TIMEOUT_SECONDS = 1800 in app.workers.worker) plus grace.
PROCESSING_STALE_AFTER = timedelta(seconds=1800 + 300)
PENDING_STALE_AFTER = timedelta(hours=6)
LEFTOVER_AFTER = timedelta(hours=1)
MAINTENANCE_INTERVAL_MINUTES = 5
MAINTENANCE_TIMEOUT_SECONDS = 300

INTERRUPTED_MESSAGE = "Ingestion was interrupted; please try again"
NOT_STARTED_MESSAGE = "Ingestion was not started; please try again"

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UPLOAD_NAME = re.compile(rf"^(?P<id>{_UUID})\.zip$")
STORAGE_NAME = re.compile(rf"^(?P<id>{_UUID})$")
LEFTOVER_DIR_NAME = re.compile(rf"^\.(?P<kind>staging|old)-(?P<id>{_UUID})-[0-9a-f]{{32}}$")

_ACTIVE = (AnalysisJobStatus.PENDING, AnalysisJobStatus.PROCESSING)


@dataclass
class MaintenanceReport:
    processing_jobs_failed: int = 0
    pending_jobs_failed: int = 0
    uploads_removed: int = 0
    leftover_dirs_removed: int = 0
    storage_dirs_removed: int = 0


# --- Database ----------------------------------------------------------------------------


async def _fail_stale_processing_jobs(db: AsyncSession) -> int:
    cutoff = func.now() - PROCESSING_STALE_AFTER
    failed = await db.execute(
        update(AnalysisJob)
        .where(
            AnalysisJob.job_type == AnalysisJobType.INGESTION,
            AnalysisJob.status == AnalysisJobStatus.PROCESSING,
            or_(
                AnalysisJob.started_at < cutoff,
                and_(AnalysisJob.started_at.is_(None), AnalysisJob.created_at < cutoff),
            ),
        )
        .values(
            status=AnalysisJobStatus.FAILED,
            error_message=INTERRUPTED_MESSAGE,
            completed_at=func.now(),
        )
        .returning(AnalysisJob.repository_id)
        .execution_options(synchronize_session=False)
    )
    rows = failed.all()
    count = len(rows)
    repository_ids = {row[0] for row in rows if row[0] is not None}
    if repository_ids:
        # Only repositories still claimed for processing change; the file snapshot stays.
        await db.execute(
            update(Repository)
            .where(
                Repository.id.in_(repository_ids),
                Repository.status == RepositoryStatus.PROCESSING,
            )
            .values(status=RepositoryStatus.FAILED)
            .execution_options(synchronize_session=False)
        )
    await db.commit()
    return count


async def _fail_stale_pending_jobs(db: AsyncSession) -> int:
    failed = await db.execute(
        update(AnalysisJob)
        .where(
            AnalysisJob.job_type == AnalysisJobType.INGESTION,
            AnalysisJob.status == AnalysisJobStatus.PENDING,
            AnalysisJob.created_at < func.now() - PENDING_STALE_AFTER,
        )
        .values(
            status=AnalysisJobStatus.FAILED,
            error_message=NOT_STARTED_MESSAGE,
            completed_at=func.now(),
        )
        .returning(AnalysisJob.id)
        .execution_options(synchronize_session=False)
    )
    count = len(failed.all())
    await db.commit()
    return count


# --- Filesystem --------------------------------------------------------------------------


def _is_link(path: Path, st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or os.path.isjunction(path)


def _remove_child(root: Path, name: str, expect_dir: bool, older_than: float | None) -> bool:
    """Remove the direct child ``name`` of ``root`` if it is safe and old enough.

    Links are never followed: a symlink or junction is removed as a link entry only.
    """
    path = root / name
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    if older_than is not None and st.st_mtime > older_than:
        return False
    if _is_link(path, st):
        if os.path.isjunction(path):
            os.rmdir(path)  # removes the junction itself, not its target
        else:
            os.unlink(path)  # removes the symlink itself, not its target
        return True
    if path.resolve().parent != root.resolve():
        return False  # never act outside the storage root
    if expect_dir and stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)  # does not follow links found inside the directory
        return True
    if not expect_dir and stat.S_ISREG(st.st_mode):
        os.unlink(path)
        return True
    return False  # unexpected entry type


def _is_real_dir(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and not _is_link(path, st)


def _old_dir_is_garbage(root: Path, repository_id: uuid.UUID, repository_exists: bool) -> bool:
    """Whether a listed ``.old-<id>-*`` directory can no longer be needed.

    ``swap_into_place`` renames the final directory to ``.old-*`` (a rename keeps its old
    mtime, so the age check does not protect it) and then moves staging into place, or on
    failure renames ``.old-*`` back. A job that started after the active-job snapshot is not
    in that snapshot, so the only safe signal is the filesystem itself: the final directory
    can only exist again once this ``.old-*`` directory was superseded (swap done) or
    restored (then it no longer exists). Checked right before removal, after listing.
    """
    return not repository_exists or _is_real_dir(root / str(repository_id))


def _children(root: Path) -> list[str]:
    """Names of the root's direct children; nothing if the root is missing or a link."""
    try:
        if os.path.islink(root) or os.path.isjunction(root) or not root.is_dir():
            return []
        return [entry.name for entry in os.scandir(root)]
    except OSError:
        return []


def _sweep(
    root: Path,
    pattern: re.Pattern[str],
    should_remove: Callable[[re.Match[str]], bool],
    expect_dir: bool,
    older_than: float | None,
) -> int:
    removed = 0
    for name in _children(root):
        match = pattern.match(name)
        if not match or not should_remove(match):
            continue
        try:
            removed += _remove_child(root, name, expect_dir, older_than)
        except OSError:
            continue  # skip entries that cannot be removed; retried on the next run
    return removed


async def _cleanup_files(db: AsyncSession, report: MaintenanceReport) -> None:
    settings = get_settings()
    uploads = ingestion_uploads_root(settings)
    repositories = repositories_root(settings)
    active = (
        await db.execute(
            select(AnalysisJob.id, AnalysisJob.repository_id).where(
                AnalysisJob.job_type == AnalysisJobType.INGESTION,
                AnalysisJob.status.in_(_ACTIVE),
            )
        )
    ).all()
    active_jobs = {row[0] for row in active}
    active_repositories = {row[1] for row in active if row[1] is not None}

    # Listed before the existence check, so a repository created afterwards is never a
    # candidate.
    names = await run_in_threadpool(_children, repositories)
    storage_ids = {uuid.UUID(m["id"]) for name in names if (m := STORAGE_NAME.match(name))}
    leftover_ids = {uuid.UUID(m["id"]) for name in names if (m := LEFTOVER_DIR_NAME.match(name))}
    candidates = storage_ids | leftover_ids
    existing = (
        set(await db.scalars(select(Repository.id).where(Repository.id.in_(candidates))))
        if candidates
        else set()
    )
    orphaned = storage_ids - existing - active_repositories
    leftover_cutoff = time.time() - LEFTOVER_AFTER.total_seconds()

    def remove_upload(match: re.Match[str]) -> bool:
        return uuid.UUID(match["id"]) not in active_jobs

    def remove_leftover(match: re.Match[str]) -> bool:
        repository_id = uuid.UUID(match["id"])
        if repository_id in active_repositories or repository_id not in leftover_ids:
            return False
        if match["kind"] == "old":
            return _old_dir_is_garbage(repositories, repository_id, repository_id in existing)
        return True  # staging is created fresh; jobs end (timeout) well before it is stale

    def remove_storage(match: re.Match[str]) -> bool:
        return uuid.UUID(match["id"]) in orphaned

    report.uploads_removed = await run_in_threadpool(
        _sweep, uploads, UPLOAD_NAME, remove_upload, False, leftover_cutoff
    )
    report.leftover_dirs_removed = await run_in_threadpool(
        _sweep, repositories, LEFTOVER_DIR_NAME, remove_leftover, True, leftover_cutoff
    )
    report.storage_dirs_removed = await run_in_threadpool(
        _sweep, repositories, STORAGE_NAME, remove_storage, True, None
    )


# --- Entry points ------------------------------------------------------------------------


async def run_maintenance(db: AsyncSession) -> MaintenanceReport:
    report = MaintenanceReport()
    report.processing_jobs_failed = await _fail_stale_processing_jobs(db)
    report.pending_jobs_failed = await _fail_stale_pending_jobs(db)
    await _cleanup_files(db, report)
    return report


async def maintenance_task(ctx: dict[str, Any]) -> str:
    """arq cron entry point; uses the worker's session factory."""
    async with ctx["session_factory"]() as db:
        await run_maintenance(db)
    return "ok"
