import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from prism.core.context import RequestContext
from prism.core.errors import PrismError
from prism.core.governance.budgets import (
    Budget,
    BudgetBatch,
    BudgetMode,
    BudgetPeriod,
    BudgetScope,
    BudgetSpend,
    BudgetTracker,
)
from prism.core.types import ChatMessage, ChatRequest


async def test_a_failed_flush_retries_with_the_latest_spend() -> None:
    first_write_started = asyncio.Event()
    fail_first_write = asyncio.Event()
    now = datetime(2026, 11, 24, tzinfo=UTC)
    budget = Budget(
        "budget", BudgetScope.KEY, "key", Decimal(10), BudgetMode.HARD, BudgetPeriod.DAILY
    )
    reset_at = budget.period.next_reset(now)
    writes: list[BudgetBatch] = []

    class Store:
        async def exhausted(self, ids: list[str], now: datetime) -> tuple[str, datetime] | None:
            return None

        async def load(self) -> list[tuple[Budget, BudgetSpend]]:
            return [(budget, BudgetSpend(budget.id, reset_at, Decimal(0)))]

        async def save(self, batch: BudgetBatch) -> None:
            writes.append(batch)
            if len(writes) == 1:
                first_write_started.set()
                await fail_first_write.wait()
                raise OSError("temporary write failure")

    service = BudgetTracker(Store(), now=lambda: now, flush_interval_s=0.005)
    request = ChatRequest(model="chat", messages=[ChatMessage(role="user", content="hi")])
    ctx = RequestContext(request, "chat", key_id="key", cost_usd=Decimal(1))
    await service.start()
    try:
        first = asyncio.create_task(service.settle(ctx))
        await asyncio.wait_for(first_write_started.wait(), timeout=2)
        second = asyncio.create_task(service.settle(ctx))
        fail_first_write.set()
        with pytest.raises(PrismError, match="settlement unavailable"):
            await first
        await asyncio.wait_for(second, timeout=2)
    finally:
        fail_first_write.set()
        await service.aclose()

    assert len(writes) == 3
    assert writes[0] == writes[1]
    assert writes[2].id != writes[1].id
    assert [batch.deltas[0].amount for batch in writes] == [Decimal(1)] * 3
