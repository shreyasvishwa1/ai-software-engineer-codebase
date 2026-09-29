import uuid
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.repository import Repository


class File(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "files"
    # One row per path within a repository. Its index (repository_id, path) also serves
    # lookups by repository_id alone, so no separate repository_id index is needed.
    __table_args__ = (UniqueConstraint("repository_id", "path"),)

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)  # POSIX path relative to repo root
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    extension: Mapped[str | None] = mapped_column(String(32))
    language: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))

    repository: Mapped["Repository"] = relationship(back_populates="files")
