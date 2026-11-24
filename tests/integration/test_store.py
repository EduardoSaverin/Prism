"""The database schema, checked through the real migration."""

import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from prism.store.db import make_engine, make_session_factory
from prism.store.migrate import current_revision, to_sync_url, upgrade
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


@pytest.fixture
def legacy_sqlite(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(
            """
            CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
            INSERT INTO alembic_version VALUES ('0001');
            CREATE TABLE teams (id VARCHAR(32) PRIMARY KEY, name VARCHAR(128) NOT NULL UNIQUE);
            INSERT INTO teams VALUES ('team', 'Platform');
            CREATE TABLE virtual_keys (
                id VARCHAR(32) NOT NULL PRIMARY KEY, key_hash VARCHAR(128) NOT NULL UNIQUE,
                key_prefix VARCHAR(16) NOT NULL, team_id VARCHAR(32) NOT NULL,
                allowed_routes JSON NOT NULL, rpm INTEGER, tpm INTEGER,
                expires_at DATETIME, revoked_at DATETIME,
                FOREIGN KEY(team_id) REFERENCES teams (id) ON DELETE CASCADE
            );
            INSERT INTO virtual_keys VALUES (
                'key', 'hash', 'pk-prism-abc1234', 'team', '["chat"]',
                25, 200, '2030-01-02 03:04:05', NULL
            );
            CREATE TABLE usage_events (
                id INTEGER NOT NULL PRIMARY KEY, ts DATETIME NOT NULL,
                request_id VARCHAR(64) NOT NULL, key_id VARCHAR(32), route VARCHAR(128) NOT NULL,
                provider VARCHAR(64) NOT NULL, model VARCHAR(256) NOT NULL,
                input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                cost_usd NUMERIC(18, 8) NOT NULL, latency_ms INTEGER NOT NULL,
                status VARCHAR(32) NOT NULL, cache_hit BOOLEAN NOT NULL
            );
            CREATE INDEX ix_usage_key_ts ON usage_events (key_id, ts);
            CREATE INDEX ix_usage_route_ts ON usage_events (route, ts);
            CREATE INDEX ix_usage_ts ON usage_events (ts);
            INSERT INTO usage_events VALUES (
                42, '2026-11-18 12:05:00', 'req', 'key', 'chat', 'provider', 'model',
                100, 50, 0.00456789, 123, 'success', 0
            ), (
                43, '2026-11-18 12:05:01', 'req2', 'deleted-key', 'chat', 'provider', 'model',
                5, 2, 0.001, 55, 'success', 0
            );
            CREATE TABLE audit_log (id INTEGER PRIMARY KEY, action VARCHAR(64) NOT NULL);
            INSERT INTO audit_log VALUES (1, 'created');
            CREATE TABLE budgets (
                id VARCHAR(32) NOT NULL PRIMARY KEY, scope VARCHAR(8) NOT NULL,
                scope_id VARCHAR(32) NOT NULL, limit_usd NUMERIC(18, 6) NOT NULL,
                period VARCHAR(8) NOT NULL, mode VARCHAR(4) NOT NULL,
                reset_at DATETIME NOT NULL, spent_usd NUMERIC(18, 6) NOT NULL,
                CONSTRAINT ck_budget_scope CHECK (scope IN ('key', 'team')),
                CONSTRAINT ck_budget_period CHECK (period IN ('daily', 'weekly', 'monthly')),
                CONSTRAINT ck_budget_mode CHECK (mode IN ('hard', 'soft'))
            );
            CREATE INDEX ix_budgets_scope ON budgets (scope, scope_id);
            INSERT INTO budgets VALUES (
                'budget', 'team', 'team', 10.5, 'monthly', 'hard', '2030-01-01 00:00:00', 1.123456
            );
            """
        )
    return path


def test_upgrading_legacy_sqlite_preserves_data(legacy_sqlite: Path) -> None:
    url = f"sqlite:///{legacy_sqlite}"
    with closing(sqlite3.connect(legacy_sqlite)) as connection:
        old_key = connection.execute("SELECT * FROM virtual_keys").fetchone()
        old_events = connection.execute("SELECT * FROM usage_events ORDER BY id").fetchall()
        old_budgets = connection.execute("SELECT * FROM budgets").fetchall()

    upgrade(url)
    upgrade(url)

    assert current_revision(url) == "0003"
    with closing(sqlite3.connect(legacy_sqlite)) as connection:
        key = connection.execute("SELECT * FROM virtual_keys").fetchone()
        assert key is not None
        assert key[:9] == old_key
        assert key[9] == "pk-prism-abc1234"
        assert key[10]
        assert key[10] == key[11]
        rows = connection.execute("SELECT * FROM usage_events ORDER BY id").fetchall()
        assert rows == [
            ("42", *old_events[0][1:], "team", 0, None),
            ("43", *old_events[1][1:], None, 0, None),
        ]
        assert connection.execute("SELECT * FROM budgets").fetchall() == old_budgets
        assert connection.execute("SELECT * FROM teams").fetchall() == [("team", "Platform")]
        assert connection.execute("SELECT * FROM audit_log").fetchall() == [(1, "created")]
        assert {row[1] for row in connection.execute("PRAGMA index_list(usage_events)")} >= {
            "ix_usage_events_ts",
            "ix_usage_events_team_id_ts",
            "ix_usage_events_key_id_ts",
            "ix_usage_route_ts",
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_legacy_sqlite_rolls_back_a_failed_upgrade(legacy_sqlite: Path) -> None:
    with closing(sqlite3.connect(legacy_sqlite)) as connection:
        # Fail at the final index creation, after both tables have been rebuilt.
        connection.execute("CREATE INDEX ix_usage_events_ts ON audit_log (id)")
        before = list(connection.iterdump())

    with pytest.raises(OperationalError, match="index ix_usage_events_ts already exists"):
        upgrade(f"sqlite:///{legacy_sqlite}")

    with closing(sqlite3.connect(legacy_sqlite)) as connection:
        assert list(connection.iterdump()) == before


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
