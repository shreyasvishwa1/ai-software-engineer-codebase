import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.analysis_job import AnalysisJob
from app.models.base import Base

FILE_METRIC_STATUSES = ("analyzed", "binary", "too_large", "unavailable")


class FileMetric(Base):
    """Code metrics of one registered file (one row per file of the analyzed snapshot).

    Keyed by the file row: re-ingestion replaces all files rows, so the metrics of a
    previous snapshot are removed with them (ON DELETE CASCADE) and can never be shown for
    a newer snapshot.
    """

    __tablename__ = "file_metrics"
    __table_args__ = (
        CheckConstraint(
            "status IN ('analyzed', 'binary', 'too_large', 'unavailable')", name="status_valid"
        ),
        # Line counts exist exactly for analyzed files, and are never negative.
        CheckConstraint(
            "(status = 'analyzed') = (line_count IS NOT NULL AND blank_line_count IS NOT NULL)"
            " AND (line_count IS NULL OR line_count >= 0)"
            " AND (blank_line_count IS NULL OR blank_line_count BETWEEN 0 AND line_count)",
            name="counts_match_status",
        ),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True, nullable=False
    )
    analysis_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("analysis_jobs.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    line_count: Mapped[int | None] = mapped_column(Integer)
    blank_line_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# At most one active code-metrics job per repository. Declared with the code-metrics model
# (the analysis_jobs model is unchanged) and attached to analysis_jobs; created by 0003.
Index(
    "uq_analysis_jobs_active_code_metrics",
    AnalysisJob.repository_id,
    unique=True,
    postgresql_where=text("job_type = 'code_metrics' AND status IN ('pending', 'processing')"),
)
