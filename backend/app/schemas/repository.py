import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.enums import RepositorySourceType, RepositoryStatus

# Match the repositories.name / default_branch columns (String(255)).
MAX_REPOSITORY_NAME_LENGTH = 255
MAX_DEFAULT_BRANCH_LENGTH = 255
# source_url is a Text column; cap it to keep request payloads reasonable.
MAX_SOURCE_URL_LENGTH = 2048


def _clean_required(value: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("must not be empty")
    if len(cleaned) > max_length:
        raise ValueError(f"must be at most {max_length} characters")
    return cleaned


def _clean_optional(value: str | None, max_length: int) -> str | None:
    return None if value is None else _clean_required(value, max_length)


class RepositoryCreate(BaseModel):
    # Unknown fields (e.g. id, project_id, user_id, status, timestamps) are rejected:
    # the project comes from the URL and ownership from the authenticated user.
    model_config = ConfigDict(extra="forbid")

    name: str
    source_type: RepositorySourceType
    source_url: str | None = None
    default_branch: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_required(value, MAX_REPOSITORY_NAME_LENGTH)

    @field_validator("source_url")
    @classmethod
    def _validate_source_url(cls, value: str | None) -> str | None:
        return _clean_optional(value, MAX_SOURCE_URL_LENGTH)

    @field_validator("default_branch")
    @classmethod
    def _validate_default_branch(cls, value: str | None) -> str | None:
        return _clean_optional(value, MAX_DEFAULT_BRANCH_LENGTH)


class RepositoryUpdate(BaseModel):
    """Partial update: omitted fields are left unchanged.

    source_url and default_branch may be set to null; name may not. source_type is
    fixed at creation, and status is managed by the server.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    source_url: str | None = None
    default_branch: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("must not be null")
        return _clean_required(value, MAX_REPOSITORY_NAME_LENGTH)

    @field_validator("source_url")
    @classmethod
    def _validate_source_url(cls, value: str | None) -> str | None:
        return _clean_optional(value, MAX_SOURCE_URL_LENGTH)

    @field_validator("default_branch")
    @classmethod
    def _validate_default_branch(cls, value: str | None) -> str | None:
        return _clean_optional(value, MAX_DEFAULT_BRANCH_LENGTH)


class RepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    source_type: RepositorySourceType
    source_url: str | None
    default_branch: str | None
    status: RepositoryStatus
    created_at: datetime
    updated_at: datetime
