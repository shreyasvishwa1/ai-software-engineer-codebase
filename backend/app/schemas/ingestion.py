import uuid

from pydantic import BaseModel

from app.models.enums import AnalysisJobStatus


class IngestionJobResponse(BaseModel):
    """Returned when ingestion is queued (202); track it via the repository's jobs."""

    job_id: uuid.UUID
    repository_id: uuid.UUID
    status: AnalysisJobStatus
