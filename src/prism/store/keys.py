"""Virtual keys in the database.

The gateway reads keys through `SqlKeyLookup`. The CLI manages keys
with the functions below. `KeyChangeWatcher` lets running instances notice keys
that were changed or revoked by another process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from prism.core.governance.keys import (
    KeyCache,
    KeyRecord,
    check_key_name,
    display_prefix,
    generate_key,
    hash_key,
)
from prism.store.admin import AdminError, Sessions
from prism.store.models import Team, VirtualKey
from prism.store.teams import team_by_name

log = logging.getLogger(__name__)


def to_record(row: VirtualKey) -> KeyRecord:
    return KeyRecord(
        id=row.id,
        team_id=row.team_id,
        name=row.name,
        allowed_routes=tuple(row.allowed_routes),
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


class SqlKeyLookup:
    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions

    async def find_by_hash(self, key_hash: str) -> KeyRecord | None:
        async with self.sessions() as session:
            row: VirtualKey | None = await session.scalar(
                select(VirtualKey).where(VirtualKey.key_hash == key_hash)
            )
        return to_record(row) if row else None


# ---------- Management, used by the CLI ----------


@dataclass(frozen=True)
class KeyListing:
    key: VirtualKey
    team_name: str

    @property
    def status(self) -> str:
        if self.key.revoked_at is not None:
            return "revoked"
        expires = self.key.expires_at
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= datetime.now(UTC):
                return "expired"
        return "active"


@dataclass(frozen=True)
class CreatedKey:
    secret: str
    key: VirtualKey


async def create_key(
    sessions: Sessions,
    *,
    pepper: str,
    team_name: str,
    name: str,
    routes: Sequence[str] = (),
    expires_at: datetime | None = None,
) -> CreatedKey:
    try:
        check_key_name(name)
    except ValueError as exc:
        raise AdminError(str(exc)) from exc
    secret = generate_key()
    now = datetime.now(UTC)
    async with sessions() as session, session.begin():
        team = await team_by_name(session, team_name)
        key = VirtualKey(
            key_hash=hash_key(secret, pepper),
            key_prefix=display_prefix(secret),
            name=name,
            team_id=team.id,
            allowed_routes=list(routes),
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        session.add(key)
    return CreatedKey(secret, key)


async def list_keys(sessions: Sessions, team_name: str | None = None) -> list[KeyListing]:
    async with sessions() as session:
        query = (
            select(VirtualKey, Team.name)
            .join(Team, Team.id == VirtualKey.team_id)
            .order_by(Team.name, VirtualKey.created_at)
        )
        if team_name is not None:
            await team_by_name(session, team_name)
            query = query.where(Team.name == team_name)
        rows = (await session.execute(query)).all()
    return [KeyListing(key, team) for key, team in rows]


async def revoke_key(sessions: Sessions, ident: str) -> VirtualKey:
    """Revoke a key by its id or its display prefix. Revoking twice is harmless."""
    async with sessions() as session, session.begin():
        matches = list(
            await session.scalars(
                select(VirtualKey).where(
                    or_(VirtualKey.id == ident, VirtualKey.key_prefix == ident)
                )
            )
        )
        if not matches:
            raise AdminError(f"No key has the id or prefix '{ident}'.")
        if len(matches) > 1:
            raise AdminError(f"More than one key starts with '{ident}'. Use the key id instead.")
        key = matches[0]
        if key.revoked_at is None:
            now = datetime.now(UTC)
            key.revoked_at = now
            key.updated_at = now
    return key


# ---------- Noticing changes made by other processes ----------


class KeyChangeWatcher:
    """Polls for keys whose `updated_at` changed and evicts them from the cache.

    The CLI runs in its own process, so it cannot reach an instance's in-memory
    cache. Polling bounds how long a revoked key keeps working to about
    `interval_s`. Each poll looks back `margin_s` further than the newest change
    it has seen, to allow for clocks that differ between machines.
    """

    def __init__(
        self,
        sessions: Sessions,
        cache: KeyCache,
        *,
        interval_s: float = 2.0,
        margin_s: float = 10.0,
        lookback_s: float = 300.0,
    ) -> None:
        self.sessions = sessions
        self.cache = cache
        self.interval_s = interval_s
        self.margin = timedelta(seconds=margin_s)
        # A shared cache (Redis) may hold entries from before this instance started.
        self.since = datetime.now(UTC) - timedelta(seconds=lookback_s)
        self._task: asyncio.Task[None] | None = None

    async def poll_once(self) -> int:
        """Evict every key changed since the last poll. Returns how many."""
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(VirtualKey.key_hash, VirtualKey.updated_at).where(
                        VirtualKey.updated_at >= self.since - self.margin
                    )
                )
            ).all()
        for key_hash, updated_at in rows:
            await self.cache.evict(key_hash)
            seen = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
            self.since = max(self.since, seen)
        return len(rows)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            try:
                await self.poll_once()
            except Exception:
                log.warning("Could not check for changed virtual keys", exc_info=True)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="prism-key-watcher")

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
