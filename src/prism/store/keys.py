"""Reading virtual keys from the database on behalf of running instances.

`SqlKeyLookup` finds a key for the authenticator. `SqlKeyChangeSource` lists
keys changed by another process, for the key watcher. Managing keys is in
`prism.store.key_admin`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from prism.core.governance.keys import KeyChange, KeyRecord
from prism.store.admin import Sessions
from prism.store.models import VirtualKey


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


class SqlKeyChangeSource:
    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions

    async def changed_since(self, since: datetime) -> list[KeyChange]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(VirtualKey.key_hash, VirtualKey.updated_at).where(
                        VirtualKey.updated_at >= since
                    )
                )
            ).all()
        return [KeyChange(key_hash, _as_utc(updated_at)) for key_hash, updated_at in rows]


def _as_utc(moment: datetime) -> datetime:
    """SQLite gives back naive datetimes. Prism stores UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
