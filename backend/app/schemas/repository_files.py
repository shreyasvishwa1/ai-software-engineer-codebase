import uuid
from typing import Literal

from pydantic import BaseModel

from app.models.enums import RepositoryStatus

# Read-only views over a repository's registered files. Deliberately excluded: file ids,
# sha256 checksums, file contents and filesystem paths.


class TreeFile(BaseModel):
    type: Literal["file"] = "file"
    name: str
    path: str  # POSIX path relative to the repository root
    extension: str | None
    language: str | None
    size_bytes: int


class TreeDirectory(BaseModel):
    type: Literal["directory"] = "directory"
    name: str  # "" for the repository root
    path: str  # "" for the repository root
    children: list["TreeDirectory | TreeFile"]


TreeDirectory.model_rebuild()


class RepositoryTreeResponse(BaseModel):
    repository_id: uuid.UUID
    status: RepositoryStatus
    file_count: int
    total_bytes: int
    tree: TreeDirectory


class LanguageStatistic(BaseModel):
    language: str | None  # None: no detected language
    file_count: int
    total_bytes: int
    percentage: float  # share of total_bytes, rounded to 2 decimals


class RepositoryLanguagesResponse(BaseModel):
    repository_id: uuid.UUID
    status: RepositoryStatus
    total_files: int
    total_bytes: int
    languages: list[LanguageStatistic]
