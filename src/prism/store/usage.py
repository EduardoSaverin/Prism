"""Usage records in the database: the default UsageSink (ADR 0010)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy import insert

from prism.core.usage.record import UsageRecord
from prism.store.admin import Sessions
from prism.store.models import UsageEvent, new_uuid


class SqlUsageSink:
    """Writes each batch of usage records with one multi-row insert."""

    def __init__(self, sessions: Sessions) -> None:
        self.sessions = sessions

    async def write(self, records: Sequence[UsageRecord]) -> None:
        if not records:
            return
        rows = [{"id": new_uuid(), **asdict(record)} for record in records]
        async with self.sessions() as session, session.begin():
            await session.execute(insert(UsageEvent), rows)
