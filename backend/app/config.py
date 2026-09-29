from pydantic_settings import BaseSettings
from functools import lru_cache

class Settings(BaseSettings):
    app_name: str = "AI Software Engineer"
    app_version: str = "1.0.0"
    debug: bool = True
    api_prefix: str = "/api"

    # Database - defaults to sqlite for Phase 1 dev without postgres installed
    # Will switch to postgres in Phase 2 via .env
    database_url: str = "sqlite:///./ai_software_engineer.db"

    # CORS
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Security
    secret_key: str = "dev-secret-key-change-in-production"
    max_upload_size_mb: int = 100

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

@lru_cache
def get_settings() -> Settings:
    return Settings()
