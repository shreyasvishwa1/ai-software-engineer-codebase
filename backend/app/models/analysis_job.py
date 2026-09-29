import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from app.models.enums import AnalysisJobStatus

if TYPE_CHECKING:
    from app.models.project import Project


class AnalysisJob(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (CheckConstraint("progress BETWEEN 0 AND 100", name="progress_range"),)

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[AnalysisJobStatus] = mapped_column(
        Enum(
            AnalysisJobStatus,
            name="analysis_job_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=AnalysisJobStatus.QUEUED,
        server_default=AnalysisJobStatus.QUEUED.value,
        nullable=False,
    )
    progress: Mapped[int] = mapped_column(
        SmallInteger, default=0, server_default="0", nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped["Project"] = relationship(back_populates="analysis_jobs")
