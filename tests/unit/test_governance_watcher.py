"""The key watcher's polling policy, with a fake change source."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from prism.core.governance.keys import InMemoryKeyCache, KeyChange, KeyRecord
from prism.core.governance.watcher import KeyChangeWatcher

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class FakeSource:
    def __init__(self) -> None:
        self.changes: list[KeyChange] = []
        self.asked: list[datetime] = []
        self.fail = False
        self.asked_twice = asyncio.Event()

    async def changed_since(self, since: datetime) -> list[KeyChange]:
        self.asked.append(since)
        if len(self.asked) >= 2:
            self.asked_twice.set()
        if self.fail:
            raise ConnectionError("database down")
        return [c for c in self.changes if c.updated_at >= since]


def make_watcher(source: FakeSource, cache: InMemoryKeyCache, **kwargs: float) -> KeyChangeWatcher:
    return KeyChangeWatcher(source, cache, now=lambda: START, **kwargs)


async def test_the_first_poll_looks_back_and_allows_for_clock_skew() -> None:
    source = FakeSource()
    await make_watcher(source, InMemoryKeyCache(), lookback_s=300, margin_s=10).poll_once()
    assert source.asked == [START - timedelta(seconds=310)]


async def test_changed_keys_are_evicted() -> None:
    source, cache = FakeSource(), InMemoryKeyCache()
    await cache.set("a", KeyRecord(id="a", team_id="t"))
    await cache.set("b", KeyRecord(id="b", team_id="t"))
    source.changes = [KeyChange("a", START)]

    assert await make_watcher(source, cache).poll_once() == 1
    assert await cache.get("a") is None
    assert await cache.get("b") is not None


async def test_successful_polls_advance_the_window_even_when_no_keys_change() -> None:
    source = FakeSource()
    now = START
    watcher = KeyChangeWatcher(source, InMemoryKeyCache(), now=lambda: now)
    source.changes = [KeyChange("a", START)]
    assert await watcher.poll_once() == 1
    now += timedelta(seconds=12)
    assert await watcher.poll_once() == 1  # the previous poll's overlap still includes a
    now += timedelta(seconds=2)
    assert await watcher.poll_once() == 0
    assert watcher.since == now


async def test_an_older_change_does_not_move_the_window_back() -> None:
    source = FakeSource()
    watcher = make_watcher(source, InMemoryKeyCache(), lookback_s=0, margin_s=10)
    source.changes = [KeyChange("late", START - timedelta(seconds=5))]  # inside the margin

    await watcher.poll_once()
    assert watcher.since == START


async def test_a_failed_poll_is_logged_and_the_loop_keeps_going(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = FakeSource()
    source.fail = True
    watcher = make_watcher(source, InMemoryKeyCache(), interval_s=0.001)
    await watcher.start()
    await asyncio.wait_for(source.asked_twice.wait(), timeout=2)  # it polled again after failing
    await watcher.aclose()
    assert source.asked[0] == source.asked[1]
    assert "Could not check for changed virtual keys" in caplog.text


async def test_starting_and_stopping_twice_is_harmless() -> None:
    watcher = make_watcher(FakeSource(), InMemoryKeyCache(), interval_s=0.01)
    await watcher.start()
    await watcher.start()
    await watcher.aclose()
    await watcher.aclose()
