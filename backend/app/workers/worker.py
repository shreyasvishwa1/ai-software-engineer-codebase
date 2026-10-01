"""arq worker for repository ingestion.

Run from backend/ (Redis and PostgreSQL must be reachable via REDIS_URL/DATABASE_URL):

    arq app.workers.worker.WorkerSettings

No automatic retries (max_tries=1) and no stored results (keep_result=0). There is no
heartbeat. Interrupted work is recovered by the ``maintenance`` cron job (at startup and
every 5 minutes, see app.services.maintenance): an ingestion job left ``processing`` past
JOB_TIMEOUT_SECONDS plus a grace period, or ``pending`` for hours, is marked failed (the
repository leaves ``processing``, so ingestion can be requested again), and leftover
uploads and staging/old directories are removed. Failed jobs are not retried.
"""

import uuid
from typing import Any

from arq.cron import cron
from arq.worker import func

from app.database import get_engine, get_sessionmaker
from app.services.ingestion import run_ingestion_job
from app.services.maintenance import (
    MAINTENANCE_INTERVAL_MINUTES,
    MAINTENANCE_TIMEOUT_SECONDS,
    maintenance_task,
)
from app.workers.queue import INGESTION_TASK, deserialize_job, redis_settings, serialize_job

# Upper bound for one job (download total timeout is 300 s, plus extraction). A job that
# exceeds it is cancelled by arq and later marked failed by maintenance (see module docstring).
JOB_TIMEOUT_SECONDS = 1800


async def ingest_repository(ctx: dict[str, Any], analysis_job_id: str) -> str:
    """Run one ingestion job. The payload is only the AnalysisJob id; everything else is
    loaded and re-validated from PostgreSQL. Never raises for ingestion failures (they are
    recorded on the job), so arq never retries or stores exception data."""
    try:
        job_id = uuid.UUID(str(analysis_job_id))
    except ValueError:
        return "invalid"
    async with ctx["session_factory"]() as db:
        return await run_ingestion_job(db, job_id)


async def startup(ctx: dict[str, Any]) -> None:
    if "session_factory" not in ctx:
        ctx["session_factory"] = get_sessionmaker()
        ctx["owns_engine"] = True


async def shutdown(ctx: dict[str, Any]) -> None:
    if ctx.get("owns_engine"):
        await get_engine().dispose()


class WorkerSettings:
    functions = [func(ingest_repository, name=INGESTION_TASK)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = redis_settings()
    job_serializer = serialize_job
    job_deserializer = deserialize_job
    max_tries = 1
    keep_result = 0
    job_timeout = JOB_TIMEOUT_SECONDS
    cron_jobs = [
        cron(
            maintenance_task,
            name="maintenance",
            minute=set(range(0, 60, MAINTENANCE_INTERVAL_MINUTES)),
            second=0,
            run_at_startup=True,
            unique=True,
            timeout=MAINTENANCE_TIMEOUT_SECONDS,
            keep_result=0,
            max_tries=1,
        )
    ]
