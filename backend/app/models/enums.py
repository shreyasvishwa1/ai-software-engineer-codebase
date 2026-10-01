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
    # Renamed from queued/running in migration 0002 (same meaning).
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisJobType(enum.StrEnum):
    INGESTION = "ingestion"
    CODE_METRICS = "code_metrics"  # added in migration 0003
