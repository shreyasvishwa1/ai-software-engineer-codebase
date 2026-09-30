import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import AnalysisJobStatus, AnalysisJobType

# Read-only: jobs are created and updated only by application services, so there is no
# client-facing create/update schema.


class AnalysisJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    repository_id: uuid.UUID
    job_type: AnalysisJobType
    status: AnalysisJobStatus
    progress: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
