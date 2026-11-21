from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import String

from prism.core.errors import ErrorKind, PrismError
from prism.core.governance.keys import (
    DISPLAY_PREFIX_LENGTH,
    KEY_PREFIX,
    Authenticator,
    InMemoryKeyCache,
    KeyRecord,
    check_key_name,
    check_pepper,
    display_prefix,
    generate_key,
    hash_key,
)
from prism.store.models import VirtualKey

PEPPER = "p" * 32


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class CountingLookup:
    def __init__(self, records: dict[str, KeyRecord] | None = None, fail: bool = False) -> None:
        self.records = records or {}
        self.fail = fail
        self.calls = 0

    async def find_by_hash(self, key_hash: str) -> KeyRecord | None:
        self.calls += 1
        if self.fail:
            raise ConnectionError("database is down")
        return self.records.get(key_hash)


def test_generated_keys_have_the_prefix_and_are_unique() -> None:
    keys = {generate_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(k.startswith(KEY_PREFIX) and len(k) == len(KEY_PREFIX) + 43 for k in keys)


def test_display_prefix_shows_eight_random_characters() -> None:
    key = generate_key()
    assert display_prefix(key) == key[: len(KEY_PREFIX) + 8]
    assert len(KEY_PREFIX) + 8 == DISPLAY_PREFIX_LENGTH


def test_display_prefix_fits_the_key_prefix_column() -> None:
    column_type = VirtualKey.__table__.c.key_prefix.type
    assert isinstance(column_type, String)
    assert column_type.length is not None
    assert column_type.length >= DISPLAY_PREFIX_LENGTH


def test_hash_depends_on_the_key_and_the_pepper() -> None:
    key = generate_key()
    assert hash_key(key, PEPPER) == hash_key(key, PEPPER)
    assert hash_key(key, PEPPER) != hash_key(key, "q" * 32)
    assert hash_key(key, PEPPER) != hash_key(generate_key(), PEPPER)
    assert len(hash_key(key, PEPPER)) == 64


@pytest.mark.parametrize("name", ["notebook", "Data team notebook", "ci-bot_2", "x" * 128])
def test_valid_key_names_are_accepted(name: str) -> None:
    assert check_key_name(name) == name


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", "cannot be empty"),
        ("   ", "cannot be empty"),
        ("x" * 129, "at most 128"),
        (" notebook", "start or end with a space"),
        ("notebook ", "start or end with a space"),
        ("note\nbook", "control characters"),
        ("note\tbook", "control characters"),
    ],
)
def test_invalid_key_names_are_refused(name: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        check_key_name(name)


@pytest.mark.parametrize("pepper", [None, "", "short"])
def test_a_missing_or_short_pepper_is_refused(pepper: str | None) -> None:
    with pytest.raises(ValueError, match="PRISM_KEY_PEPPER"):
        check_pepper(pepper)


def test_record_round_trips_through_a_dict_and_gains_utc() -> None:
    naive = datetime(2030, 1, 1, 12, 0)
    record = KeyRecord(id="k", team_id="t", name="app", allowed_routes=("a", "b"), expires_at=naive)
    assert record.expires_at == naive.replace(tzinfo=UTC)
    assert KeyRecord.from_dict(record.to_dict()) == record


def test_an_empty_route_list_allows_every_route() -> None:
    assert KeyRecord(id="k", team_id="t").allows("anything")
    limited = KeyRecord(id="k", team_id="t", allowed_routes=("chat",))
    assert limited.allows("chat")
    assert not limited.allows("other")


async def test_memory_cache_stores_evicts_and_expires() -> None:
    clock = FakeClock()
    cache = InMemoryKeyCache(ttl_s=10, clock=clock)
    record = KeyRecord(id="k", team_id="t")
    await cache.set("h", record)
    assert await cache.get("h") == record
    clock.now = 10
    assert await cache.get("h") is None
    await cache.set("h", record)
    await cache.evict("h")
    await cache.evict("missing")
    assert await cache.get("h") is None


async def test_memory_cache_drops_the_least_recently_used_entry() -> None:
    cache = InMemoryKeyCache(max_entries=2)
    for name in ("a", "b"):
        await cache.set(name, KeyRecord(id=name, team_id="t"))
    await cache.get("a")
    await cache.set("c", KeyRecord(id="c", team_id="t"))
    assert len(cache) == 2
    assert await cache.get("b") is None
    assert await cache.get("a") is not None


async def test_authenticate_uses_the_cache_after_the_first_lookup() -> None:
    secret = generate_key()
    record = KeyRecord(id="k", team_id="t")
    lookup = CountingLookup({hash_key(secret, PEPPER): record})
    auth = Authenticator(lookup, InMemoryKeyCache(), PEPPER)
    assert await auth.authenticate(secret) == record
    assert await auth.authenticate(secret) == record
    assert lookup.calls == 1


@pytest.mark.parametrize("token", [None, "", "Bearer x", "sk-openai-key", KEY_PREFIX + "unknown"])
async def test_bad_tokens_are_unauthenticated(token: str | None) -> None:
    auth = Authenticator(CountingLookup(), InMemoryKeyCache(), PEPPER)
    with pytest.raises(PrismError) as info:
        await auth.authenticate(token)
    assert info.value.kind is ErrorKind.UNAUTHENTICATED


async def test_tokens_without_the_prefix_never_reach_the_database() -> None:
    lookup = CountingLookup()
    auth = Authenticator(lookup, InMemoryKeyCache(), PEPPER)
    with pytest.raises(PrismError):
        await auth.authenticate("sk-not-ours")
    assert lookup.calls == 0


async def test_unknown_keys_are_not_cached() -> None:
    lookup = CountingLookup()
    cache = InMemoryKeyCache()
    auth = Authenticator(lookup, cache, PEPPER)
    for _ in range(2):
        with pytest.raises(PrismError):
            await auth.authenticate(generate_key())
    assert len(cache) == 0


async def test_revoked_keys_are_cached_and_refused() -> None:
    secret = generate_key()
    revoked = KeyRecord(id="k", team_id="t", revoked_at=datetime.now(UTC))
    lookup = CountingLookup({hash_key(secret, PEPPER): revoked})
    auth = Authenticator(lookup, InMemoryKeyCache(), PEPPER)
    for _ in range(2):
        with pytest.raises(PrismError, match="revoked"):
            await auth.authenticate(secret)
    assert lookup.calls == 1


async def test_a_key_stops_working_when_it_expires() -> None:
    secret = generate_key()
    now = datetime(2030, 1, 1, tzinfo=UTC)
    record = KeyRecord(id="k", team_id="t", expires_at=now + timedelta(seconds=1))
    clock = [now]
    auth = Authenticator(
        CountingLookup({hash_key(secret, PEPPER): record}),
        InMemoryKeyCache(),
        PEPPER,
        now=lambda: clock[0],
    )
    assert await auth.authenticate(secret) == record
    clock[0] = now + timedelta(seconds=1)
    with pytest.raises(PrismError, match="expired"):
        await auth.authenticate(secret)


async def test_a_broken_key_store_is_an_internal_error() -> None:
    auth = Authenticator(CountingLookup(fail=True), InMemoryKeyCache(), PEPPER)
    with pytest.raises(PrismError) as info:
        await auth.authenticate(generate_key())
    assert info.value.kind is ErrorKind.INTERNAL_ERROR
    assert "database" not in info.value.client_message


def test_authorize_refuses_routes_outside_the_list() -> None:
    record = KeyRecord(id="k", team_id="t", allowed_routes=("chat",))
    Authenticator.authorize(record, "chat")
    with pytest.raises(PrismError) as info:
        Authenticator.authorize(record, "other")
    assert info.value.kind is ErrorKind.PERMISSION_DENIED
    assert info.value.http_status == 403


def test_the_authenticator_checks_the_pepper() -> None:
    with pytest.raises(ValueError, match="PRISM_KEY_PEPPER"):
        Authenticator(CountingLookup(), InMemoryKeyCache(), "short")
