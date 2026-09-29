import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import RepositorySourceType, RepositoryStatus

if TYPE_CHECKING:
    from app.models.file import File
    from app.models.project import Project


class Repository(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "repositories"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[RepositorySourceType] = mapped_column(
        Enum(
            RepositorySourceType,
            name="repository_source_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    source_url: Mapped[str | None] = mapped_column(Text)
    default_branch: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[RepositoryStatus] = mapped_column(
        Enum(
            RepositoryStatus,
            name="repository_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=RepositoryStatus.PENDING,
        server_default=RepositoryStatus.PENDING.value,
        nullable=False,
    )

    project: Mapped["Project"] = relationship(back_populates="repositories")
    files: Mapped[list["File"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan", passive_deletes=True
    )
