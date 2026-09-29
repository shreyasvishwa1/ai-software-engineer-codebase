import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.analysis_job import AnalysisJob
    from app.models.repository import Repository
    from app.models.user import User


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    owner: Mapped["User"] = relationship(back_populates="projects")
    repositories: Mapped[list["Repository"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    analysis_jobs: Mapped[list["AnalysisJob"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
