"""The database schema, checked through the real migration."""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from prism.store.db import make_engine, make_session_factory
from prism.store.migrate import to_sync_url, upgrade
from prism.store.models import Base, Team, VirtualKey

POSTGRES_URL = os.environ.get("PRISM_TEST_POSTGRES_URL")


def _reset_postgres(url: str) -> None:
    engine = create_engine(to_sync_url(url))
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def db_url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    if request.param == "sqlite":
        url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    elif POSTGRES_URL:
        url = POSTGRES_URL
        _reset_postgres(url)
    else:
        pytest.skip("set PRISM_TEST_POSTGRES_URL to run the PostgreSQL tests")
    upgrade(url)
    return url


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(db_url)
    yield make_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def sync_engine(db_url: str) -> Iterator[Engine]:
    engine = create_engine(to_sync_url(db_url))
    yield engine
    engine.dispose()


async def save(sessions: async_sessionmaker[AsyncSession], *rows: Base) -> None:
    """Save some rows in one transaction."""
    async with sessions() as session, session.begin():
        session.add_all(rows)


def test_migration_creates_every_table(sync_engine: Engine) -> None:
    created = set(inspect(sync_engine).get_table_names())
    assert set(Base.metadata.tables) <= created
    assert "alembic_version" in created


def test_migration_matches_the_models_exactly(sync_engine: Engine) -> None:
    with sync_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_running_the_migration_twice_is_harmless(db_url: str) -> None:
    upgrade(db_url)


def test_url_conversion_for_migrations() -> None:
    assert to_sync_url("sqlite+aiosqlite:///x.db") == "sqlite:///x.db"
    assert to_sync_url("postgresql+asyncpg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert to_sync_url("sqlite:///x.db") == "sqlite:///x.db"


async def test_rows_can_be_saved_and_read_back(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    soon = datetime.now(UTC) + timedelta(days=1)
    await save(sessions, Team(id="team1", name="Platform"))
    await save(
        sessions,
        VirtualKey(
            id="vk1",
            key_hash="h" * 64,
            key_prefix="pk-prism-abcd",
            name="notebook",
            team_id="team1",
            allowed_routes=["fast-chat"],
            expires_at=soon,
        ),
    )
    async with sessions() as session:
        key = (await session.execute(select(VirtualKey))).scalar_one()
        assert key.allowed_routes == ["fast-chat"]
        assert key.name == "notebook"
        assert key.revoked_at is None
        assert key.expires_at is not None
        assert key.created_at is not None


async def test_foreign_keys_are_enforced(sessions: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(IntegrityError):
        await save(
            sessions, VirtualKey(key_hash="x", key_prefix="p", name="k", team_id="no-such-team")
        )


async def test_deleting_a_team_removes_its_keys(sessions: async_sessionmaker[AsyncSession]) -> None:
    async with sessions() as session, session.begin():
        session.add(Team(id="t1", name="Temp"))
        await session.flush()
        session.add(VirtualKey(id="vk1", key_hash="h1", key_prefix="p", name="k", team_id="t1"))
    async with sessions() as session, session.begin():
        team = await session.get(Team, "t1")
        assert team is not None
        await session.delete(team)
    async with sessions() as session:
        assert (await session.execute(select(VirtualKey))).first() is None
