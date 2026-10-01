"""Cancelling ingestion jobs that have not started yet.

Only ``pending`` jobs can be cancelled. Cancellation reuses ``fail_pending_job``: one
conditional UPDATE (pending -> failed), so it races safely with the worker's conditional
claim (pending -> processing): exactly one of them matches the row. A cancelled job is a
``failed`` job with CANCELLED_MESSAGE; a pending job never changed its repository's status,
so the repository is not touched. The queued arq entry is left in Redis: when it is
delivered, the worker cannot claim the job and skips it (removing any leftover upload).

The database change is authoritative. Removing the job's uploaded ZIP afterwards is best
effort: if it fails (e.g. a Windows file lock), the cancellation stands and the Step 16
maintenance task removes the upload of the no-longer-active job later.
"""

import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.services.analysis_jobs import fail_pending_job
from app.services.ingestion import ingestion_artifact_path

CANCELLED_MESSAGE = "Ingestion was cancelled"


def _remove_upload(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)  # GitHub jobs have no upload
    except OSError:
        pass  # left for maintenance; the cancellation is already committed


async def cancel_pending_ingestion_job(db: AsyncSession, job_id: uuid.UUID) -> bool:
    """Cancel ``job_id`` if it is still pending. Returns False (and changes nothing) when
    it is not: claimed by a worker, already finished, or gone. The caller must have
    authorized the job; its upload path is derived from the id only."""
    if not await fail_pending_job(db, job_id, CANCELLED_MESSAGE):
        await db.rollback()
        return False
    await db.commit()
    await run_in_threadpool(_remove_upload, ingestion_artifact_path(get_settings(), job_id))
    return True
