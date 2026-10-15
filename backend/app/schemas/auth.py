import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

MIN_PASSWORD_LENGTH = 8
MAX_EMAIL_LENGTH = 320


def normalize_email(value: str) -> str:
    """Trim and lower-case an email, and check its basic shape (local@domain.tld)."""
    email = value.strip().lower()
    local, sep, domain = email.partition("@")
    if (
        not sep
        or not local
        or "@" in domain
        or "." not in domain
        or domain.startswith(".")
        or domain.endswith(".")
        or any(ch.isspace() for ch in email)
        or len(email) > MAX_EMAIL_LENGTH
    ):
        raise ValueError("must be a valid email address")
    return email


class _Credentials(BaseModel):
    email: str
    # SecretStr keeps the password out of reprs and logs. Its length is checked in the
    # register route rather than here, so validation errors never echo it back.
    password: SecretStr

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class RegisterRequest(_Credentials):
    pass


class LoginRequest(_Credentials):
    pass


class PasswordResetRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email(value)


class PasswordResetConfirm(BaseModel):
    token: SecretStr
    # Length is checked in the route, so validation errors never echo the password back.
    password: SecretStr


class MessageResponse(BaseModel):
    detail: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    created_at: datetime
    updated_at: datetime
