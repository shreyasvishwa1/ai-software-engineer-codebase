import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import AnalysisJobStatus, AnalysisJobType

if TYPE_CHECKING:
    from app.models.project import Project
    from app.models.repository import Repository


class AnalysisJob(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress_range"),
        # Rows written after migration 0002 must name their repository and type. NOT VALID:
        # rows created before 0002 keep NULLs instead of being assigned guessed values.
        CheckConstraint(
            "repository_id IS NOT NULL AND job_type IS NOT NULL",
            name="repository_and_type_required",
            postgresql_not_valid=True,
        ),
        # Repository job listings (newest first); also serves lookups by repository_id.
        Index("ix_analysis_jobs_repository_id_created_at", "repository_id", "created_at"),
        # At most one active ingestion job per repository.
        Index(
            "uq_analysis_jobs_active_ingestion",
            "repository_id",
            unique=True,
            postgresql_where=text(
                "job_type = 'ingestion' AND status IN ('pending', 'processing')"
            ),
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Nullable only for rows created before migration 0002 (see the check constraint).
    repository_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    job_type: Mapped[AnalysisJobType | None] = mapped_column(
        Enum(
            AnalysisJobType,
            name="analysis_job_type",
            values_callable=lambda e: [m.value for m in e],
        )
    )
    status: Mapped[AnalysisJobStatus] = mapped_column(
        Enum(
            AnalysisJobStatus,
            name="analysis_job_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=AnalysisJobStatus.PENDING,
        server_default=AnalysisJobStatus.PENDING.value,
        nullable=False,
    )
    progress: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default="0", nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped["Project"] = relationship(back_populates="analysis_jobs")
    repository: Mapped["Repository | None"] = relationship(back_populates="analysis_jobs")
