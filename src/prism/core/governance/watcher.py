"""Evicting cached keys that another process changed.

The CLI runs in its own process, so it cannot reach an instance's in-memory
cache. Each instance polls a `KeyChangeSource` instead, which bounds how long
a revoked key keeps working to about `interval_s`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from prism.core.governance.keys import KeyCache, KeyChangeSource, Now

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class KeyChangeWatcher:
    """Polls for changed keys and evicts them from the cache.

    Each poll looks back `margin_s` before the previous successful poll's start,
    to allow for clocks that differ between machines. The first poll looks back
    `lookback_s`, because a shared cache (Redis) may hold entries from before
    this instance started.
    """

    def __init__(
        self,
        source: KeyChangeSource,
        cache: KeyCache,
        *,
        interval_s: float = 2.0,
        margin_s: float = 10.0,
        lookback_s: float = 300.0,
        now: Now = _utcnow,
    ) -> None:
        self.source = source
        self.cache = cache
        self.interval_s = interval_s
        self.margin = timedelta(seconds=margin_s)
        self._now = now
        self.since = now() - timedelta(seconds=lookback_s)
        self._task: asyncio.Task[None] | None = None

    async def poll_once(self) -> int:
        """Evict every key changed since the last poll. Returns how many."""
        started_at = self._now()
        changes = await self.source.changed_since(self.since - self.margin)
        for change in changes:
            await self.cache.evict(change.key_hash)
        self.since = started_at
        return len(changes)

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
