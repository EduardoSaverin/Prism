"""Virtual keys in a real (SQLite) database."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy import update

from prism.core.governance.keys import (
    DISPLAY_PREFIX_LENGTH,
    InMemoryKeyCache,
    KeyRecord,
    hash_key,
)
from prism.core.governance.watcher import KeyChangeWatcher
from prism.main import build_governance, connect_database
from prism.store.admin import AdminError, Sessions
from prism.store.db import make_engine, make_session_factory
from prism.store.key_admin import KeyListing, create_key, list_keys, revoke_key
from prism.store.keys import SqlKeyChangeSource, SqlKeyLookup
from prism.store.migrate import upgrade
from prism.store.models import VirtualKey
from prism.store.redis_cache import KEY_NAMESPACE, RedisKeyCache
from prism.store.team_admin import create_team

PEPPER = "p" * 32


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'keys.db'}"
    upgrade(url)
    return url


@pytest.fixture
async def sessions(db_url: str) -> AsyncIterator[Sessions]:
    engine = make_engine(db_url)
    yield make_session_factory(engine)
    await engine.dispose()


async def test_a_created_key_can_be_found_by_its_hash(sessions: Sessions) -> None:
    team = await create_team(sessions, "ml")
    created = await create_key(
        sessions, pepper=PEPPER, team_name="ml", name="notebook", routes=["chat"]
    )
    assert created.key.key_prefix == created.secret[:DISPLAY_PREFIX_LENGTH]
    assert created.secret not in created.key.key_hash

    record = await SqlKeyLookup(sessions).find_by_hash(hash_key(created.secret, PEPPER))
    assert record == KeyRecord(
        id=created.key.id, team_id=team.id, name="notebook", allowed_routes=("chat",)
    )
    assert await SqlKeyLookup(sessions).find_by_hash("0" * 64) is None


async def test_a_key_needs_an_existing_team(sessions: Sessions) -> None:
    with pytest.raises(AdminError, match="no team named 'ghost'"):
        await create_key(sessions, pepper=PEPPER, team_name="ghost", name="x")


@pytest.mark.parametrize("name", ["", " ", "a\nb"])
async def test_an_invalid_key_name_creates_no_key(sessions: Sessions, name: str) -> None:
    await create_team(sessions, "ml")
    with pytest.raises(AdminError, match="key name"):
        await create_key(sessions, pepper=PEPPER, team_name="ml", name=name)
    assert await list_keys(sessions) == []


async def test_keys_are_listed_with_their_team_and_status(sessions: Sessions) -> None:
    await create_team(sessions, "ml")
    await create_team(sessions, "data")
    past = datetime.now(UTC) - timedelta(days=1)
    a = await create_key(sessions, pepper=PEPPER, team_name="ml", name="a")
    await create_key(sessions, pepper=PEPPER, team_name="ml", name="b", expires_at=past)
    await create_key(sessions, pepper=PEPPER, team_name="data", name="c")
    await revoke_key(sessions, a.key.id)

    everything = await list_keys(sessions)
    assert [(k.team_name, k.key.name, k.status) for k in everything] == [
        ("data", "c", "active"),
        ("ml", "a", "revoked"),
        ("ml", "b", "expired"),
    ]
    assert [k.key.name for k in await list_keys(sessions, "data")] == ["c"]
    with pytest.raises(AdminError):
        await list_keys(sessions, "ghost")


async def test_revoke_by_id_or_prefix(sessions: Sessions) -> None:
    await create_team(sessions, "ml")
    a = await create_key(sessions, pepper=PEPPER, team_name="ml", name="a")
    b = await create_key(sessions, pepper=PEPPER, team_name="ml", name="b")

    revoked = await revoke_key(sessions, a.key.id)
    assert revoked.revoked_at is not None
    first = revoked.revoked_at.replace(tzinfo=None)  # SQLite reads dates back without a zone
    again = (await revoke_key(sessions, a.key.id)).revoked_at
    assert again is not None
    assert again.replace(tzinfo=None) == first  # revoking twice keeps the first time

    assert (await revoke_key(sessions, b.key.key_prefix)).id == b.key.id
    with pytest.raises(AdminError, match="No key"):
        await revoke_key(sessions, "pk-prism-none")


async def test_an_ambiguous_prefix_is_refused(sessions: Sessions) -> None:
    await create_team(sessions, "ml")
    a = await create_key(sessions, pepper=PEPPER, team_name="ml", name="a")
    b = await create_key(sessions, pepper=PEPPER, team_name="ml", name="b")
    async with sessions() as session, session.begin():
        await session.execute(
            update(VirtualKey).where(VirtualKey.id == b.key.id).values(key_prefix=a.key.key_prefix)
        )
    with pytest.raises(AdminError, match="More than one"):
        await revoke_key(sessions, a.key.key_prefix)


def test_listing_status_handles_naive_dates() -> None:
    key = VirtualKey(expires_at=datetime(2000, 1, 1))
    assert KeyListing(key, "t").status == "expired"


async def test_the_change_source_lists_keys_changed_since_a_moment(sessions: Sessions) -> None:
    await create_team(sessions, "ml")
    created = await create_key(sessions, pepper=PEPPER, team_name="ml", name="a")
    key_hash = hash_key(created.secret, PEPPER)
    source = SqlKeyChangeSource(sessions)
    before = datetime.now(UTC) - timedelta(minutes=1)

    [change] = await source.changed_since(before)
    assert change.key_hash == key_hash
    assert change.updated_at.tzinfo is not None  # SQLite's naive dates come back as UTC
    assert await source.changed_since(datetime.now(UTC) + timedelta(minutes=1)) == []

    await revoke_key(sessions, created.key.id)
    [revoked] = await source.changed_since(change.updated_at + timedelta(microseconds=1))
    assert revoked.key_hash == key_hash


async def test_the_watcher_evicts_a_key_revoked_in_the_database(sessions: Sessions) -> None:
    await create_team(sessions, "ml")
    created = await create_key(sessions, pepper=PEPPER, team_name="ml", name="a")
    key_hash = hash_key(created.secret, PEPPER)
    cache = InMemoryKeyCache()
    watcher = KeyChangeWatcher(SqlKeyChangeSource(sessions), cache)
    await watcher.poll_once()

    await cache.set(key_hash, KeyRecord(id=created.key.id, team_id="t"))
    await revoke_key(sessions, created.key.id)
    await watcher.poll_once()
    assert await cache.get(key_hash) is None


async def test_redis_cache_round_trip_and_eviction() -> None:
    client = FakeAsyncRedis()
    cache = RedisKeyCache(client, ttl_s=60)
    record = KeyRecord(id="k", team_id="t", allowed_routes=("chat",))
    assert await cache.get("h") is None
    await cache.set("h", record)
    assert await cache.get("h") == record
    assert 0 < await client.ttl(KEY_NAMESPACE + "h") <= 60
    await cache.evict("h")
    assert await cache.get("h") is None


async def test_redis_cache_ignores_unreadable_entries() -> None:
    client = FakeAsyncRedis()
    await client.set(KEY_NAMESPACE + "h", json.dumps({"no": "id"}))
    assert await RedisKeyCache(client).get("h") is None


class BrokenRedis:
    async def get(self, *args: Any) -> None:
        raise ConnectionError("down")

    async def set(self, *args: Any, **kwargs: Any) -> None:
        raise ConnectionError("down")

    async def delete(self, *args: Any) -> None:
        raise ConnectionError("down")


async def test_redis_errors_are_cache_misses() -> None:
    cache = RedisKeyCache(BrokenRedis())  # type: ignore[arg-type]
    assert await cache.get("h") is None
    await cache.set("h", KeyRecord(id="k", team_id="t"))
    await cache.evict("h")


async def test_build_governance_uses_memory_or_redis(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"PRISM_KEY_PEPPER": PEPPER, "PRISM_DATABASE_URL": db_url}
    database = connect_database(env)
    memory = build_governance(env, database)
    assert isinstance(memory.authenticator.cache, InMemoryKeyCache)
    for service in memory.services:
        await service.start()
    for service in memory.services:
        await service.aclose()

    monkeypatch.setattr("prism.store.redis_cache.connect", lambda url: FakeAsyncRedis())
    shared = build_governance({**env, "PRISM_REDIS_URL": "redis://localhost:6379/0"}, database)
    assert isinstance(shared.authenticator.cache, RedisKeyCache)
    for service in shared.services:
        await service.aclose()
    await database.aclose()
