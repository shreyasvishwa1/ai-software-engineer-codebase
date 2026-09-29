"""Async SQLAlchemy engine and session management.

The engine is created lazily on first use, so importing this module (or starting
the app) never opens a connection or requires a valid database URL.
"""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import ConfigurationError, get_settings

REQUIRED_DRIVER = "postgresql+asyncpg"


def get_database_url() -> URL:
    """Parse and validate DATABASE_URL. Error messages never include the URL itself."""
    raw = get_settings().database_url.get_secret_value()
    try:
        url = make_url(raw)
    except ArgumentError:
        raise ConfigurationError("DATABASE_URL is not a valid database URL") from None
    if url.drivername != REQUIRED_DRIVER:
        raise ConfigurationError(f"DATABASE_URL must use the {REQUIRED_DRIVER}:// scheme")
    return url


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        get_database_url(),
        echo=get_settings().debug,
        pool_pre_ping=True,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, always closed afterwards.

    Closing the session rolls back any transaction the caller did not commit.
    """
    async with get_sessionmaker()() as session:
        yield session
