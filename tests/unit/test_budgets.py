import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from prism.core.context import RequestContext
from prism.core.governance.budgets import (
    Budget,
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
    retry_succeeded = asyncio.Event()
    now = datetime(2026, 11, 24, tzinfo=UTC)
    budget = Budget(
        "budget", BudgetScope.KEY, "key", Decimal(10), BudgetMode.HARD, BudgetPeriod.DAILY
    )
    reset_at = budget.period.next_reset(now)
    writes: list[BudgetSpend] = []

    class Store:
        async def load(self) -> list[tuple[Budget, BudgetSpend]]:
            return [(budget, BudgetSpend(budget.id, reset_at, Decimal(0)))]

        async def save(self, snapshots: Sequence[BudgetSpend]) -> None:
            writes.extend(snapshots)
            if len(writes) == 1:
                first_write_started.set()
                await fail_first_write.wait()
                raise OSError("temporary write failure")
            retry_succeeded.set()

    service = BudgetTracker(Store(), now=lambda: now, flush_interval_s=0.005)
    request = ChatRequest(model="chat", messages=[ChatMessage(role="user", content="hi")])
    ctx = RequestContext(request, "chat", key_id="key", cost_usd=Decimal(1))
    await service.start()
    try:
        service.charge(ctx)
        await asyncio.wait_for(first_write_started.wait(), timeout=2)
        service.charge(ctx)
        fail_first_write.set()
        await asyncio.wait_for(retry_succeeded.wait(), timeout=2)
    finally:
        fail_first_write.set()
        await service.aclose()

    assert [snapshot.spent_usd for snapshot in writes] == [Decimal(1), Decimal(2)]
