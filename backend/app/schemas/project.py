import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

# Matches the projects.name column (String(100)).
MAX_PROJECT_NAME_LENGTH = 100


def _clean_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("must not be empty")
    if len(name) > MAX_PROJECT_NAME_LENGTH:
        raise ValueError(f"must be at most {MAX_PROJECT_NAME_LENGTH} characters")
    return name


class ProjectCreate(BaseModel):
    # Unknown fields (e.g. user_id, owner_id, id, timestamps) are rejected:
    # ownership always comes from the authenticated user.
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_name(value)


class ProjectUpdate(BaseModel):
    """Partial update: omitted fields are left unchanged; description may be set to null."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("must not be null")
        return _clean_name(value)


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
