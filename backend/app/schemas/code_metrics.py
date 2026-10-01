import uuid
from typing import Literal

from pydantic import BaseModel

from app.models.enums import AnalysisJobStatus
from app.schemas.analysis_job import AnalysisJobResponse

# Code metrics of a repository's current snapshot. Deliberately excluded: file ids, sha256
# checksums, file contents and filesystem paths.

FileMetricStatus = Literal["analyzed", "binary", "too_large", "unavailable"]


class CodeMetricsJobResponse(BaseModel):
    job_id: uuid.UUID
    repository_id: uuid.UUID
    status: AnalysisJobStatus


class FileMetricResponse(BaseModel):
    path: str  # POSIX path relative to the repository root
    language: str | None
    status: FileMetricStatus
    line_count: int | None  # set only for analyzed files
    blank_line_count: int | None


class CodeMetricsSummary(BaseModel):
    files: int
    analyzed: int
    binary: int
    too_large: int
    unavailable: int
    total_lines: int
    blank_lines: int


class CodeMetricsResponse(BaseModel):
    job: AnalysisJobResponse | None  # the latest code-metrics job, if any
    summary: CodeMetricsSummary | None  # None until the current snapshot is analyzed
    files: list[FileMetricResponse]
