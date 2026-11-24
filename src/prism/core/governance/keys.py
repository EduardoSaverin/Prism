"""Virtual keys: their format, hashing, caching and checking.

A virtual key is the bearer token a client sends to Prism. Only an HMAC of it is
stored, keyed with a server secret (the pepper). See ADR 0008.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from prism.core.errors import ErrorKind, PrismError

log = logging.getLogger(__name__)

KEY_PREFIX = "pk-prism-"
DISPLAY_PREFIX_LENGTH = len(KEY_PREFIX) + 8
MIN_PEPPER_LENGTH = 32
MAX_KEY_NAME_LENGTH = 128

_INVALID_KEY = "Invalid or missing API key."


def generate_key() -> str:
    """A new random key: the prefix and 256 bits of URL-safe randomness."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def display_prefix(key: str) -> str:
    """The short, non-secret start of a key, used to tell keys apart."""
    return key[:DISPLAY_PREFIX_LENGTH]


def hash_key(key: str, pepper: str) -> str:
    """HMAC-SHA256 of the key, keyed with the server pepper, as hex."""
    return hmac.new(pepper.encode(), key.encode(), hashlib.sha256).hexdigest()


def check_key_name(name: str) -> str:
    """Return the name, or raise ValueError if it is not a valid key name.

    A key name is a label, such as the app that uses the key, so spaces are
    allowed inside it. It must be 1 to 128 printable characters, with no space
    at the start or end.
    """
    if not name.strip():
        raise ValueError("A key name cannot be empty.")
    if len(name) > MAX_KEY_NAME_LENGTH:
        raise ValueError(f"A key name can be at most {MAX_KEY_NAME_LENGTH} characters.")
    if name != name.strip():
        raise ValueError("A key name cannot start or end with a space.")
    if not name.isprintable():
        raise ValueError("A key name cannot contain control characters such as a new line.")
    return name


def check_pepper(pepper: str | None) -> str:
    """Return the pepper, or raise ValueError if it is missing or too short."""
    if not pepper:
        raise ValueError("PRISM_KEY_PEPPER is not set.")
    if len(pepper) < MIN_PEPPER_LENGTH:
        raise ValueError(f"PRISM_KEY_PEPPER must be at least {MIN_PEPPER_LENGTH} characters.")
    return pepper


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes. Prism stores UTC, so attach it."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class KeyRecord:
    """What the gateway needs to know about one virtual key."""

    id: str
    team_id: str
    name: str = ""
    allowed_routes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "expires_at", _as_utc(self.expires_at))
        object.__setattr__(self, "revoked_at", _as_utc(self.revoked_at))

    def allows(self, route: str) -> bool:
        """An empty list of allowed routes means every route."""
        return not self.allowed_routes or route in self.allowed_routes

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "name": self.name,
            "allowed_routes": list(self.allowed_routes),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyRecord:
        expires = data.get("expires_at")
        revoked = data.get("revoked_at")
        return cls(
            id=str(data["id"]),
            team_id=str(data["team_id"]),
            name=str(data.get("name", "")),
            allowed_routes=tuple(str(r) for r in data.get("allowed_routes", [])),
            expires_at=datetime.fromisoformat(expires) if expires else None,
            revoked_at=datetime.fromisoformat(revoked) if revoked else None,
        )


class KeyLookup(Protocol):
    """Finds a key by its hash in the source of truth (the database)."""

    async def find_by_hash(self, key_hash: str) -> KeyRecord | None: ...


class KeyCache(Protocol):
    """A cache of key records by hash. Must never raise: a failure is a miss."""

    async def get(self, key_hash: str) -> KeyRecord | None: ...

    async def set(self, key_hash: str, record: KeyRecord) -> None: ...

    async def evict(self, key_hash: str) -> None: ...


@dataclass(frozen=True)
class KeyChange:
    """A key that was created, changed or revoked, and when (timezone-aware UTC)."""

    key_hash: str
    updated_at: datetime


class KeyChangeSource(Protocol):
    """Lists keys changed in the source of truth at or after a moment."""

    async def changed_since(self, since: datetime) -> list[KeyChange]: ...


Clock = Callable[[], float]


class InMemoryKeyCache:
    """A bounded LRU cache for one instance.

    Entries also expire after `ttl_s`, as a safety net in case a change is
    missed by the database poller.
    """

    def __init__(
        self, *, max_entries: int = 10_000, ttl_s: float = 300.0, clock: Clock = time.monotonic
    ) -> None:
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self.clock = clock
        self._entries: OrderedDict[str, tuple[float, KeyRecord]] = OrderedDict()

    async def get(self, key_hash: str) -> KeyRecord | None:
        entry = self._entries.get(key_hash)
        if entry is None:
            return None
        stored_at, record = entry
        if self.clock() - stored_at >= self.ttl_s:
            del self._entries[key_hash]
            return None
        self._entries.move_to_end(key_hash)
        return record

    async def set(self, key_hash: str, record: KeyRecord) -> None:
        self._entries[key_hash] = (self.clock(), record)
        self._entries.move_to_end(key_hash)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    async def evict(self, key_hash: str) -> None:
        self._entries.pop(key_hash, None)

    def __len__(self) -> int:
        return len(self._entries)


Now = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Authenticator:
    """Turns a bearer token into a key record, or refuses it."""

    def __init__(
        self, lookup: KeyLookup, cache: KeyCache, pepper: str, *, now: Now = _utcnow
    ) -> None:
        self.lookup = lookup
        self.cache = cache
        self.pepper = check_pepper(pepper)
        self.now = now

    async def authenticate(self, token: str | None) -> KeyRecord:
        if not token or not token.startswith(KEY_PREFIX):
            raise PrismError(ErrorKind.UNAUTHENTICATED, _INVALID_KEY)
        key_hash = hash_key(token, self.pepper)
        record = await self.cache.get(key_hash)
        if record is None:
            record = await self._find(key_hash)
            if record is None:
                raise PrismError(ErrorKind.UNAUTHENTICATED, _INVALID_KEY)
            # Revoked keys are cached too, so they do not reach the database again.
            await self.cache.set(key_hash, record)
        now = self.now()
        if record.revoked_at is not None:
            raise PrismError(ErrorKind.UNAUTHENTICATED, "This API key has been revoked.")
        if record.expires_at is not None and record.expires_at <= now:
            raise PrismError(ErrorKind.UNAUTHENTICATED, "This API key has expired.")
        return record

    async def _find(self, key_hash: str) -> KeyRecord | None:
        try:
            return await self.lookup.find_by_hash(key_hash)
        except Exception as exc:
            log.exception("Could not look up a virtual key")
            raise PrismError(ErrorKind.INTERNAL_ERROR, "The key store is not available.") from exc

    @staticmethod
    def authorize(record: KeyRecord, route: str) -> None:
        if not record.allows(route):
            raise PrismError(
                ErrorKind.PERMISSION_DENIED,
                f"This API key may not use the model '{route}'.",
            )
