"""Usage records in a real (SQLite) database, and end to end through the app."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, inspect, select

from prism.core.usage.record import UsageRecord
from prism.main import app_from_env
from prism.store.admin import Sessions
from prism.store.db import make_engine, make_session_factory
from prism.store.key_admin import create_key
from prism.store.migrate import upgrade
from prism.store.models import UsageEvent
from prism.store.team_admin import create_team
from prism.store.usage import SqlUsageSink

PEPPER = "p" * 32


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}"
    upgrade(url)
    return url


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[Sessions]:
    engine = make_engine(db_url)
    yield make_session_factory(engine)
    await engine.dispose()


def record(request_id: str, **changes: object) -> UsageRecord:
    values: dict[str, object] = {
        "ts": datetime(2030, 1, 1, 12, tzinfo=UTC),
        "request_id": request_id,
        "route": "chat",
        "key_id": "k1",
        "team_id": "t1",
        "provider": "groq",
        "model": "m",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": Decimal("0.00000985"),
        "latency_ms": 42,
        "status": "success",
        "cache_hit": False,
        "attempts": 1,
    }
    values.update(changes)
    return UsageRecord(**values)  # type: ignore[arg-type]


async def rows(sessions: Sessions) -> list[UsageEvent]:
    async with sessions() as session:
        return list(await session.scalars(select(UsageEvent).order_by(UsageEvent.request_id)))


async def test_a_batch_is_written_as_one_row_per_record(sessions: Sessions) -> None:
    sink = SqlUsageSink(sessions)
    await sink.write(
        [
            record("r1"),
            record("r2", key_id=None, team_id=None, provider=None, model=None, status="x"),
        ]
    )
    first, second = await rows(sessions)
    assert first.cost_usd == Decimal("0.00000985")  # exact, not a float
    assert (first.input_tokens, first.output_tokens, first.latency_ms) == (10, 5, 42)
    assert (first.provider, first.model, first.status) == ("groq", "m", "success")
    assert len(first.id) == 32
    assert (second.key_id, second.provider, second.status) == (None, None, "x")


async def test_an_empty_batch_writes_nothing(sessions: Sessions) -> None:
    await SqlUsageSink(sessions).write([])
    assert await rows(sessions) == []


def test_downgrading_removes_the_usage_table(db_url: str) -> None:
    from alembic import command
    from sqlalchemy import create_engine

    from prism.store.migrate import alembic_config, to_sync_url

    command.downgrade(alembic_config(db_url), "0001")
    engine = create_engine(to_sync_url(db_url))
    try:
        assert not inspect(engine).has_table("usage_events")
        assert inspect(engine).has_table("virtual_keys")
    finally:
        engine.dispose()


async def test_every_request_through_the_app_leaves_a_usage_row(
    db_url: str, sessions: Sessions, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "prism.yaml"
    config.write_text(
        "providers:\n"
        "  - id: fake\n"
        "    kind: openai_compatible\n"
        "    base_url: http://127.0.0.1:9\n"
        "routes:\n"
        "  - name: chat\n"
        "    targets: [{provider: fake, model: m}]\n"
        "    retry: {max_retries: 0}\n"
    )
    monkeypatch.setenv("PRISM_CONFIG", str(config))
    monkeypatch.setenv("PRISM_KEY_PEPPER", PEPPER)
    monkeypatch.setenv("PRISM_DATABASE_URL", db_url)
    monkeypatch.delenv("PRISM_REDIS_URL", raising=False)
    team = await create_team(sessions, "ml")
    created = await create_key(sessions, pepper=PEPPER, team_name="ml", name="app")

    app = app_from_env()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/chat/completions",
                json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {created.secret}"},
            )
    # Nothing listens on port 9, so the provider is unavailable.
    assert response.status_code == 502
    # Shutting the app down wrote the queued record before closing the database.
    (row,) = await rows(sessions)
    assert (row.route, row.provider, row.model) == ("chat", "fake", "m")
    assert (row.key_id, row.team_id) == (created.key.id, team.id)
    assert (row.status, row.attempts, row.cost_usd) == ("upstream_unavailable", 1, 0)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(UsageEvent)) == 1
