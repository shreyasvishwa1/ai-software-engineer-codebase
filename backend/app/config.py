"""Application configuration.

All settings come from environment variables, optionally loaded from
``backend/.env`` by pydantic-settings. No other module should read the
environment or ``.env`` files directly; use ``get_settings()``.

Real environment variables take precedence over values in ``backend/.env``.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env, resolved from this file so it does not depend on the working directory.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Repository root; the default DATA_DIR is <repo>/data (git-ignored except .gitkeep).
REPO_ROOT = Path(__file__).resolve().parents[2]

MIN_SECRET_KEY_LENGTH = 32

# JWTs are signed with SECRET_KEY, a shared secret, so only HMAC algorithms apply.
# An explicit allow-list also rules out "none" (unsigned tokens).
SUPPORTED_JWT_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})

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

    # JWT access tokens (signed with SECRET_KEY)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = Field(default=60, gt=0)

    # Repository archive ingestion limits (the uploaded ZIP itself uses MAX_UPLOAD_SIZE_MB)
    max_archive_file_count: int = Field(default=10000, ge=1, le=1_000_000)
    max_extracted_size_mb: int = Field(default=500, ge=1, le=102_400)
    max_extracted_file_size_mb: int = Field(default=25, ge=1, le=102_400)
    max_compression_ratio: int = Field(default=100, ge=1, le=10_000)

    # Background job queue (arq). Treated as a secret because the URL can embed a password.
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")

    # Local storage for extracted repositories: DATA_DIR/repositories/<repository_id>/.
    # Relative paths are resolved against the repository root, not the working directory.
    data_dir: Path = REPO_ROOT / "data"

    # AI file explanations (Claude). Unset or empty disables the feature; never logged.
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"

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

    @field_validator("jwt_algorithm")
    @classmethod
    def _validate_jwt_algorithm(cls, value: str) -> str:
        if value not in SUPPORTED_JWT_ALGORITHMS:
            raise ValueError(f"must be one of {', '.join(sorted(SUPPORTED_JWT_ALGORITHMS))}")
        return value

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: SecretStr) -> SecretStr:
        # Messages never include the URL itself.
        try:
            parts = urlsplit(value.get_secret_value())
            port = parts.port
        except ValueError:
            raise ValueError("must be a valid redis:// or rediss:// URL") from None
        database = parts.path.lstrip("/")
        if (
            parts.scheme not in ("redis", "rediss")
            or not parts.hostname
            or (port is not None and not 0 < port < 65536)
            or (database and not database.isdigit())
            or parts.fragment
        ):
            raise ValueError("must be redis://[user:password@]host[:port][/db] (or rediss://)")
        return value

    @field_validator("anthropic_api_key")
    @classmethod
    def _empty_api_key_disables(cls, value: SecretStr | None) -> SecretStr | None:
        # ANTHROPIC_API_KEY= (empty, as in .env.example) means "not configured".
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @field_validator("llm_model")
    @classmethod
    def _validate_llm_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("data_dir")
    @classmethod
    def _resolve_data_dir(cls, value: Path) -> Path:
        path = value if value.is_absolute() else REPO_ROOT / value
        return path.resolve()

    @model_validator(mode="after")
    def _validate_extraction_limits(self) -> "Settings":
        if self.max_extracted_file_size_mb > self.max_extracted_size_mb:
            raise ValueError(
                "MAX_EXTRACTED_FILE_SIZE_MB must not exceed MAX_EXTRACTED_SIZE_MB"
            )
        return self

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
