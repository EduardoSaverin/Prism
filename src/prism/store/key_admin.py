"""Managing virtual keys in the database: create, list and revoke. Used by the CLI."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from prism.core.governance.keys import check_key_name, display_prefix, generate_key, hash_key
from prism.store.admin import AdminError, Sessions
from prism.store.models import Team, VirtualKey
from prism.store.team_admin import team_by_name


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
        key = await key_by_id_or_prefix(session, ident)
        if key.revoked_at is None:
            now = datetime.now(UTC)
            key.revoked_at = now
            key.updated_at = now
    return key


async def key_by_id_or_prefix(session: AsyncSession, ident: str) -> VirtualKey:
    matches = list(
        await session.scalars(
            select(VirtualKey).where(or_(VirtualKey.id == ident, VirtualKey.key_prefix == ident))
        )
    )
    if not matches:
        raise AdminError(f"No key has the id or prefix '{ident}'.")
    if len(matches) > 1:
        raise AdminError(f"More than one key starts with '{ident}'. Use the key id instead.")
    return matches[0]
