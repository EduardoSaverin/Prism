"""The database schema, checked through the real migration."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from prism.store.db import make_engine, make_session_factory
from prism.store.migrate import to_sync_url, upgrade
from prism.store.models import (
    AuditLog,
    Base,
    Budget,
    Provider,
    ProviderKey,
    Route,
    RouteTarget,
    Team,
    UsageEvent,
    VirtualKey,
)

pytestmark = pytest.mark.filterwarnings("ignore:Dialect sqlite.*Decimal:sqlalchemy.exc.SAWarning")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    upgrade(url)
    return url


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


async def test_a_full_set_of_rows_can_be_saved_and_read_back(db_url: str) -> None:
    engine = make_engine(db_url)
    sessions = make_session_factory(engine)
    soon = datetime.now(UTC) + timedelta(days=1)
    async with sessions() as session, session.begin():
        session.add_all(
            [
                Provider(id="groq", kind="groq"),
                Team(id="team1", name="Platform"),
                Route(id="route1", name="fast-chat", config_json={"retry": {"max_retries": 2}}),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ProviderKey(id="key1", provider_id="groq", secret_encrypted=b"\x00\x01"),
                RouteTarget(
                    route_id="route1", position=0, provider_id="groq", model="m", key_pool=["key1"]
                ),
                VirtualKey(
                    id="vk1",
                    key_hash="h" * 64,
                    key_prefix="prism-ab",
                    team_id="team1",
                    allowed_routes=["fast-chat"],
                ),
                Budget(
                    scope="team",
                    scope_id="team1",
                    limit_usd=Decimal("50"),
                    period="monthly",
                    reset_at=soon,
                ),
                UsageEvent(
                    request_id="r1",
                    route="fast-chat",
                    provider="groq",
                    model="m",
                    input_tokens=10,
                    output_tokens=5,
                    cost_usd=Decimal("0.00012"),
                    latency_ms=420,
                    status="ok",
                ),
                AuditLog(actor="admin", action="create_route", target="fast-chat"),
            ]
        )
    async with sessions() as session:
        key = (await session.execute(select(VirtualKey))).scalar_one()
        assert key.allowed_routes == ["fast-chat"]
        assert key.revoked_at is None
        route = (await session.execute(select(Route))).scalar_one()
        assert route.config_json == {"retry": {"max_retries": 2}}
        event = (await session.execute(select(UsageEvent))).scalar_one()
        assert event.id == 1
        assert event.cache_hit is False
        assert event.cost_usd == Decimal("0.00012000")
        budget = (await session.execute(select(Budget))).scalar_one()
        assert budget.mode == "hard"
        assert budget.spent_usd == 0
    await engine.dispose()


async def test_foreign_keys_are_enforced(db_url: str) -> None:
    engine = make_engine(db_url)
    sessions = make_session_factory(engine)
    with pytest.raises(IntegrityError):
        await save(sessions, VirtualKey(key_hash="x", key_prefix="p", team_id="no-such-team"))
    await engine.dispose()


async def test_deleting_a_team_removes_its_keys(db_url: str) -> None:
    engine = make_engine(db_url)
    sessions = make_session_factory(engine)
    async with sessions() as session, session.begin():
        session.add(Team(id="t1", name="Temp"))
        await session.flush()
        session.add(VirtualKey(id="vk1", key_hash="h1", key_prefix="p", team_id="t1"))
    async with sessions() as session, session.begin():
        team = await session.get(Team, "t1")
        assert team is not None
        await session.delete(team)
    async with sessions() as session:
        assert (await session.execute(select(VirtualKey))).first() is None
    await engine.dispose()


@pytest.mark.parametrize(
    "bad",
    [
        {"scope": "galaxy", "period": "daily", "mode": "hard"},
        {"scope": "team", "period": "hourly", "mode": "hard"},
        {"scope": "team", "period": "daily", "mode": "maybe"},
    ],
)
async def test_budget_values_are_limited(db_url: str, bad: dict[str, str]) -> None:
    engine = make_engine(db_url)
    sessions = make_session_factory(engine)
    row = Budget(scope_id="x", limit_usd=Decimal(1), reset_at=datetime.now(UTC), **bad)
    with pytest.raises(IntegrityError):
        await save(sessions, row)
    await engine.dispose()


async def test_key_status_is_limited(db_url: str) -> None:
    engine = make_engine(db_url)
    sessions = make_session_factory(engine)
    await save(sessions, Provider(id="p", kind="groq"))
    with pytest.raises(IntegrityError):
        await save(sessions, ProviderKey(provider_id="p", secret_encrypted=b"x", status="on-fire"))
    await engine.dispose()
