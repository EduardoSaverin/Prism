import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from prism.core.usage.base import UsageRecorder, UsageSink
from prism.core.usage.batching import BatchingUsageRecorder
from prism.core.usage.record import UsageRecord


def record(n: int = 0) -> UsageRecord:
    return UsageRecord(
        ts=datetime(2030, 1, 1, tzinfo=UTC),
        request_id=f"r{n}",
        route="chat",
        key_id=None,
        team_id=None,
        provider="a",
        model="m",
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal(0),
        latency_ms=1,
        status="success",
        cache_hit=False,
        attempts=1,
    )


class MemorySink:
    def __init__(self, fail_times: int = 0, delay_s: float = 0) -> None:
        self.batches: list[list[UsageRecord]] = []
        self.fail_times = fail_times
        self.delay_s = delay_s

    async def write(self, records: Sequence[UsageRecord]) -> None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("database is down")
        self.batches.append(list(records))

    @property
    def ids(self) -> list[str]:
        return [r.request_id for batch in self.batches for r in batch]


def test_the_recorder_and_sink_follow_their_contracts() -> None:
    sink = MemorySink()
    assert isinstance(sink, UsageSink)
    assert isinstance(BatchingUsageRecorder(sink), UsageRecorder)


@pytest.mark.parametrize("bad", [{"max_queue": 0}, {"batch_size": 0}, {"flush_interval_s": 0}])
def test_settings_must_be_positive(bad: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="positive"):
        BatchingUsageRecorder(MemorySink(), **bad)  # type: ignore[arg-type]


async def test_flush_writes_in_batches_of_batch_size() -> None:
    sink = MemorySink()
    recorder = BatchingUsageRecorder(sink, batch_size=2)
    for n in range(5):
        recorder.record(record(n))
    assert recorder.pending() == 5
    assert await recorder.flush() == 5
    assert [len(b) for b in sink.batches] == [2, 2, 1]
    assert sink.ids == ["r0", "r1", "r2", "r3", "r4"]
    assert await recorder.flush() == 0


async def test_a_full_queue_drops_new_records_and_warns_once_per_flush(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = MemorySink()
    recorder = BatchingUsageRecorder(sink, max_queue=2)
    for n in range(5):
        recorder.record(record(n))
    assert recorder.dropped == 3
    with caplog.at_level(logging.WARNING, logger="prism.core.usage.batching"):
        await recorder.flush()
        await recorder.flush()
    assert sink.ids == ["r0", "r1"]
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1
    assert "3 usage records were dropped" in warnings[0]


async def test_a_failed_write_drops_that_batch_and_keeps_going(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = MemorySink(fail_times=1)
    recorder = BatchingUsageRecorder(sink, batch_size=2)
    for n in range(4):
        recorder.record(record(n))
    with caplog.at_level(logging.ERROR, logger="prism.core.usage.batching"):
        assert await recorder.flush() == 2
    assert sink.ids == ["r2", "r3"]
    assert recorder.failed == 2
    assert "Could not write 2 usage records" in caplog.records[0].getMessage()


async def test_the_background_task_flushes_on_its_interval() -> None:
    sink = MemorySink()
    recorder = BatchingUsageRecorder(sink, flush_interval_s=0.01)
    await recorder.start()
    await recorder.start()  # starting twice is harmless
    recorder.record(record(1))
    for _ in range(100):
        if sink.ids:
            break
        await asyncio.sleep(0.01)
    assert sink.ids == ["r1"]
    await recorder.aclose()


async def test_a_full_batch_is_written_without_waiting_for_the_interval() -> None:
    sink = MemorySink()
    recorder = BatchingUsageRecorder(sink, batch_size=2, flush_interval_s=60)
    await recorder.start()
    recorder.record(record(1))
    recorder.record(record(2))
    for _ in range(100):
        if sink.ids:
            break
        await asyncio.sleep(0.01)
    assert sink.ids == ["r1", "r2"]
    await recorder.aclose()


async def test_closing_writes_what_is_left_and_finishes_a_write_in_progress() -> None:
    sink = MemorySink(delay_s=0.05)
    recorder = BatchingUsageRecorder(sink, batch_size=1, flush_interval_s=60)
    await recorder.start()
    recorder.record(record(1))
    await asyncio.sleep(0.01)  # the background task is now in the middle of writing r1
    recorder.record(record(2))
    await recorder.aclose()
    assert sink.ids == ["r1", "r2"]
    assert recorder.pending() == 0


async def test_closing_a_recorder_that_never_started_still_flushes() -> None:
    sink = MemorySink()
    recorder = BatchingUsageRecorder(sink)
    recorder.record(record(1))
    await recorder.aclose()
    assert sink.ids == ["r1"]
