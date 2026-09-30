import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Uuid, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app import database
from app.config import ConfigurationError, Settings
from app.models import AnalysisJob, Base, File, Project, Repository, User

BACKEND_DIR = Path(__file__).resolve().parent.parent
TABLES = {"users", "projects", "repositories", "files", "analysis_jobs"}


def test_metadata_registers_exactly_the_five_tables() -> None:
    assert set(Base.metadata.tables) == TABLES


@pytest.mark.parametrize("model", [User, Project, Repository, File, AnalysisJob])
def test_uuid_primary_keys(model: type[Base]) -> None:
    pk = list(model.__table__.primary_key.columns)
    assert [c.name for c in pk] == ["id"]
    assert isinstance(pk[0].type, Uuid)
    assert isinstance(model.__table__.c.id.default.arg(None), uuid.UUID)


def test_relationships() -> None:
    expected = {
        User: {"projects": Project},
        Project: {"owner": User, "repositories": Repository, "analysis_jobs": AnalysisJob},
        Repository: {"project": Project, "files": File, "analysis_jobs": AnalysisJob},
        File: {"repository": Repository},
        AnalysisJob: {"project": Project, "repository": Repository},
    }
    for model, rels in expected.items():
        mapper = inspect(model)
        assert {name: r.mapper.class_ for name, r in mapper.relationships.items()} == rels


def test_foreign_keys_cascade_on_delete() -> None:
    fks = {
        (fk.parent.table.name, fk.parent.name): (fk.column.table.name, fk.ondelete)
        for table in Base.metadata.tables.values()
        for fk in table.foreign_keys
    }
    assert fks == {
        ("projects", "user_id"): ("users", "CASCADE"),
        ("repositories", "project_id"): ("projects", "CASCADE"),
        ("files", "repository_id"): ("repositories", "CASCADE"),
        ("analysis_jobs", "project_id"): ("projects", "CASCADE"),
        ("analysis_jobs", "repository_id"): ("repositories", "CASCADE"),
    }


def test_unique_constraints_and_indexes() -> None:
    tables = Base.metadata.tables
    assert tables["users"].c.email.unique is True
    files_unique = {
        tuple(c.name for c in uc.columns)
        for uc in tables["files"].constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert files_unique == {("repository_id", "path")}  # also serves repository_id lookups
    indexed = {
        (t.name, tuple(c.name for c in ix.columns)) for t in tables.values() for ix in t.indexes
    }
    assert indexed == {
        ("projects", ("user_id",)),
        ("repositories", ("project_id",)),
        ("analysis_jobs", ("project_id",)),
        ("analysis_jobs", ("repository_id", "created_at")),
        ("analysis_jobs", ("repository_id",)),  # partial unique: one active ingestion job
    }


def test_status_enum_values() -> None:
    tables = Base.metadata.tables
    assert tables["repositories"].c.status.type.enums == ["pending", "processing", "ready", "failed"]
    assert tables["analysis_jobs"].c.status.type.enums == ["pending", "processing", "completed", "failed"]


def test_engine_is_async_asyncpg_and_lazy() -> None:
    engine = database.get_engine()
    assert isinstance(engine, AsyncEngine)
    assert engine.dialect.driver == "asyncpg"
    assert database.get_engine() is engine  # cached, single engine


async def test_get_db_yields_session_and_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    original_close = AsyncSession.close

    async def tracking_close(self: AsyncSession) -> None:
        closed.append(True)
        await original_close(self)

    monkeypatch.setattr(AsyncSession, "close", tracking_close)
    gen = database.get_db()
    session = await anext(gen)
    assert isinstance(session, AsyncSession)
    await gen.aclose()
    assert closed == [True]


@pytest.mark.parametrize(
    "url",
    ["sqlite:///./SECRET-MARKER.db", "postgresql://user:SECRET-MARKER@host/db", "::SECRET-MARKER::"],
)
def test_invalid_database_url_rejected_without_leaking(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    fake = Settings(_env_file=None, secret_key="x" * 40, database_url=url)  # type: ignore[call-arg]
    monkeypatch.setattr(database, "get_settings", lambda: fake)
    with pytest.raises(ConfigurationError) as excinfo:
        database.get_database_url()
    assert "SECRET-MARKER" not in str(excinfo.value)


def test_alembic_single_head_and_offline_sql_covers_the_five_tables() -> None:
    # Offline mode loads alembic/env.py and the app metadata without connecting.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND_DIR,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    created = set(re.findall(r"CREATE TABLE (\w+)", result.stdout))
    assert created == TABLES | {"alembic_version"}
    heads = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=True,
    )
    assert heads.stdout.split() == ["0002", "(head)"]


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to a dedicated, already-migrated test database",
)
async def test_migrated_database_connectivity() -> None:
    engine = create_async_engine(os.environ["TEST_DATABASE_URL"])
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
            names = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
            assert names == TABLES | {"alembic_version"}
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            assert version == "0002"
    finally:
        await engine.dispose()
