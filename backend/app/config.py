"""Application configuration.

All settings come from environment variables, optionally loaded from
``backend/.env`` by pydantic-settings. No other module should read the
environment or ``.env`` files directly; use ``get_settings()``.

Real environment variables take precedence over values in ``backend/.env``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env, resolved from this file so it does not depend on the working directory.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

MIN_SECRET_KEY_LENGTH = 32

# Publicly known placeholder values (e.g. from .env.example). Acceptable for local
# development only; rejected when ENVIRONMENT=production.
INSECURE_SECRET_KEYS = frozenset({"dev-secret-key-change-in-production"})


class ConfigurationError(RuntimeError):
    """Raised when configuration is missing or invalid. Never contains setting values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"

    app_name: str = "AI Software Engineer"
    app_version: str = "1.0.0"
    debug: bool = False
    api_prefix: str = "/api"

    # Database - defaults to sqlite for Phase 1 dev without postgres installed.
    # Treated as a secret because a server URL can embed credentials.
    database_url: SecretStr = SecretStr("sqlite:///./ai_software_engineer.db")

    # CORS: comma-separated list of exact origins, e.g. "http://localhost:5173"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Security: required, no default. Set SECRET_KEY in the environment or backend/.env.
    secret_key: SecretStr
    max_upload_size_mb: int = Field(default=100, ge=1, le=1024)

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator("api_prefix")
    @classmethod
    def _validate_api_prefix(cls, value: str) -> str:
        if not value.startswith("/") or (len(value) > 1 and value.endswith("/")):
            raise ValueError("must start with '/' and must not end with '/'")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _validate_cors_origins(cls, value: str) -> str:
        for origin in (o.strip() for o in value.split(",")):
            if not origin:
                continue
            if origin == "*":
                raise ValueError("wildcard '*' is not allowed; list exact origins")
            scheme, sep, rest = origin.partition("://")
            if not sep or scheme not in ("http", "https") or not rest or "/" in rest:
                raise ValueError(
                    "each origin must be scheme://host[:port] with http or https "
                    "and no path or trailing slash"
                )
        return value

    @field_validator("secret_key")
    @classmethod
    def _validate_secret_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(f"must be at least {MIN_SECRET_KEY_LENGTH} characters long")
        return value

    @model_validator(mode="after")
    def _validate_production(self) -> "Settings":
        if self.environment == "production":
            if self.debug:
                raise ValueError("DEBUG must be false when ENVIRONMENT=production")
            if self.secret_key.get_secret_value() in INSECURE_SECRET_KEYS:
                raise ValueError(
                    "SECRET_KEY is a published placeholder; set a unique value "
                    "when ENVIRONMENT=production"
                )
        return self


def _describe_errors(exc: ValidationError) -> str:
    """Summarize validation errors by field name and reason, without input values."""
    lines = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        field = ".".join(str(part) for part in error["loc"]).upper() or "SETTINGS"
        lines.append(f"  - {field}: {error['msg']}")
    return "Invalid application configuration:\n" + "\n".join(lines)


@lru_cache
def get_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        # "from None" drops the original exception, whose text includes input values.
        raise ConfigurationError(_describe_errors(exc)) from None
