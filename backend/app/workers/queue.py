"""Enqueueing background jobs with arq.

Queue payloads contain only an AnalysisJob UUID (as a string): never file contents, paths,
URLs, tokens or ORM objects. Jobs are serialized as JSON rather than arq's default pickle,
so a worker never unpickles data read from Redis.
"""

import json
import uuid
from typing import Any

from arq import create_pool
from arq.connections import RedisSettings

from app.config import get_settings

INGESTION_TASK = "ingest_repository"
# Fail fast when Redis is unavailable instead of arq's default retry loop.
_CONNECTION_RETRIES = 1


def serialize_job(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode()


def deserialize_job(raw: bytes) -> dict[str, Any]:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("unexpected job payload")
    return data


def redis_settings() -> RedisSettings:
    settings = RedisSettings.from_dsn(get_settings().redis_url.get_secret_value())
    settings.conn_retries = _CONNECTION_RETRIES
    return settings


async def enqueue_ingestion_job(job_id: uuid.UUID) -> None:
    """Queue the ingestion task for ``job_id``. The arq job id is the AnalysisJob id, so
    enqueueing the same job twice is a no-op."""
    pool = await create_pool(
        redis_settings(), job_serializer=serialize_job, job_deserializer=deserialize_job
    )
    try:
        await pool.enqueue_job(INGESTION_TASK, str(job_id), _job_id=str(job_id))
    finally:
        await pool.aclose()
