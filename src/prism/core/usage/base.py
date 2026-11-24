"""The contracts for recording usage and for storing it."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from prism.core.usage.record import UsageRecord


@runtime_checkable
class UsageSink(Protocol):
    """Stores a batch of usage records: a database table, a Redis stream, ..."""

    async def write(self, records: Sequence[UsageRecord]) -> None: ...


@runtime_checkable
class UsageRecorder(Protocol):
    """Accepts one record from the request path. Must return at once and never raise."""

    def record(self, record: UsageRecord) -> None: ...
