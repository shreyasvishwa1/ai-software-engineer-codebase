import uuid

from pydantic import BaseModel

from app.models.enums import RepositoryStatus


class IngestionResponse(BaseModel):
    repository_id: uuid.UUID
    status: RepositoryStatus
    file_count: int
