"""Collects usage records in memory and writes them to a sink in batches.

Requests only put a record in a bounded queue, so the database never sits on the
request path. A background task writes whatever has queued up every
`flush_interval_s`, or sooner once `batch_size` records are waiting.

Losing a record affects reports, never enforcement (budgets use live counters,
ADR 0009), so the recorder drops rather than blocks:
- a full queue drops the new record;
- a batch the sink fails to write is dropped.
A failed write is logged at once. Drops are logged at most once per flush, with
how many records were lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from prism.core.usage.base import UsageSink
from prism.core.usage.record import UsageRecord

log = logging.getLogger(__name__)

DEFAULT_MAX_QUEUE = 10_000
DEFAULT_BATCH_SIZE = 500
DEFAULT_FLUSH_INTERVAL_S = 1.0


class BatchingUsageRecorder:
    """A UsageRecorder that writes to a UsageSink in the background."""

    def __init__(
        self,
        sink: UsageSink,
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    ) -> None:
        if max_queue < 1 or batch_size < 1 or flush_interval_s <= 0:
            raise ValueError("max_queue, batch_size and flush_interval_s must be positive")
        self.sink = sink
        self.batch_size = batch_size
        self.flush_interval_s = flush_interval_s
        self._queue: asyncio.Queue[UsageRecord] = asyncio.Queue(maxsize=max_queue)
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0
        self.failed = 0
        self._reported_dropped = 0

    def record(self, record: UsageRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped += 1
            return
        if self._queue.qsize() >= self.batch_size:
            self._wake.set()

    def pending(self) -> int:
        return self._queue.qsize()

    async def flush(self) -> int:
        """Write everything queued now, in batches. Returns how many records were written."""
        written = 0
        while not self._queue.empty():
            batch = [self._queue.get_nowait() for _ in range(min(self.batch_size, self.pending()))]
            try:
                await self.sink.write(batch)
            except Exception:
                self.failed += len(batch)
                log.exception("Could not write %d usage records; they are dropped", len(batch))
            else:
                written += len(batch)
        self._report_dropped()
        return written

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="prism-usage-recorder")

    async def aclose(self) -> None:
        """Stop the background task and write what is left.

        The task is asked to stop rather than cancelled, so a batch that is being
        written when the app shuts down is not lost halfway.
        """
        if self._task is not None:
            self._stopping = True
            self._wake.set()
            await self._task
            self._task = None
        await self.flush()

    async def _run(self) -> None:
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.flush_interval_s)
            self._wake.clear()
            if not self._stopping:
                await self.flush()

    def _report_dropped(self) -> None:
        dropped = self.dropped - self._reported_dropped
        if dropped:
            self._reported_dropped = self.dropped
            log.warning(
                "The usage queue was full: %d usage records were dropped. "
                "The database may be too slow for the request rate.",
                dropped,
            )
