import enum


class RepositorySourceType(enum.StrEnum):
    ZIP = "zip"
    GITHUB = "github"


class RepositoryStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class AnalysisJobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
