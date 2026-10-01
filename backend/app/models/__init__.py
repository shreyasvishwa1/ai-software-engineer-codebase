"""SQLAlchemy models. Importing this package registers every table on ``Base.metadata``."""

from app.models.analysis_job import AnalysisJob
from app.models.base import Base
from app.models.enums import (
    AnalysisJobStatus,
    AnalysisJobType,
    RepositorySourceType,
    RepositoryStatus,
)
from app.models.file import File
from app.models.file_metric import FileMetric
from app.models.project import Project
from app.models.repository import Repository
from app.models.user import User

__all__ = [
    "AnalysisJob",
    "AnalysisJobStatus",
    "AnalysisJobType",
    "Base",
    "File",
    "FileMetric",
    "Project",
    "Repository",
    "RepositorySourceType",
    "RepositoryStatus",
    "User",
]
