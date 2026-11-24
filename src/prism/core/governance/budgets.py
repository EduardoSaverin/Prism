"""Single-process budget enforcement and periodic spend snapshots."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from prism.core.context import RequestContext
from prism.core.errors import ErrorKind, PrismError
from prism.core.governance.keys import Now

log = logging.getLogger(__name__)


class BudgetScope(StrEnum):
    KEY = "key"
    TEAM = "team"


class BudgetMode(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class BudgetPeriod(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"

    def next_reset(self, now: datetime) -> datetime:
        midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        if self is BudgetPeriod.DAILY:
            return midnight + timedelta(days=1)
        if self is BudgetPeriod.WEEKLY:
            return midnight + timedelta(days=7 - midnight.weekday())
        return (midnight.replace(day=28) + timedelta(days=4)).replace(day=1)


@dataclass(frozen=True)
class Budget:
    id: str
    scope: BudgetScope
    scope_id: str
    limit_usd: Decimal
    mode: BudgetMode
    period: BudgetPeriod


@dataclass(frozen=True)
class BudgetSpend:
    budget_id: str
    reset_at: datetime
    spent_usd: Decimal


class BudgetStore(Protocol):
    async def load(self) -> list[tuple[Budget, BudgetSpend]]: ...

    async def save(self, snapshots: Sequence[BudgetSpend]) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BudgetTracker:
    """Checks and charges budgets on the gateway's event loop without database I/O."""

    def __init__(self, store: BudgetStore, *, now: Now = _utcnow, flush_interval_s: float = 5):
        self._store = store
        self._now = now
        self._flush_interval_s = flush_interval_s
        self._budgets: dict[tuple[BudgetScope, str | None], list[Budget]] = {}
        self._spend: dict[str, BudgetSpend] = {}
        self._dirty: dict[str, BudgetSpend] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._budgets.clear()
        self._spend.clear()
        self._dirty.clear()
        self._stop.clear()
        for budget, spend in await self._store.load():
            self._budgets.setdefault((budget.scope, budget.scope_id), []).append(budget)
            self._spend[budget.id] = spend
        self._task = asyncio.create_task(self._run(), name="prism-budget-flush")

    async def aclose(self) -> None:
        if self._task is not None:
            self._stop.set()
            await self._task
            self._task = None
            await self._flush()

    def check(self, ctx: RequestContext) -> None:
        now = self._now()
        for budget in self._applicable(ctx):
            spend = self._current_spend(budget, now)
            if budget.mode is BudgetMode.HARD and spend.spent_usd >= budget.limit_usd:
                # TODO: Consider an opt-in route fallback restricted to explicitly zero-priced
                # targets when a hard budget is exhausted. Missing prices must not qualify.
                raise PrismError(
                    ErrorKind.BUDGET_EXCEEDED,
                    f"The {budget.scope} budget has been reached.",
                    retry_after_s=(spend.reset_at - now).total_seconds(),
                )

    def charge(self, ctx: RequestContext) -> int | None:
        """Charge the final cost and return the highest soft threshold crossed."""
        if ctx.cost_usd is None or ctx.cost_usd <= 0:
            return None
        now = self._now()
        threshold: int | None = None
        for budget in self._applicable(ctx):
            previous = self._current_spend(budget, now)
            spent = previous.spent_usd + ctx.cost_usd
            self._set_spend(BudgetSpend(budget.id, previous.reset_at, spent))
            if budget.mode is BudgetMode.SOFT:
                for percent in (80, 100):
                    limit = budget.limit_usd * percent / 100
                    if previous.spent_usd < limit <= spent:
                        log.warning("Soft budget %s reached %d%%", budget.id, percent)
                        threshold = max(threshold or 0, percent)
        return threshold

    def _applicable(self, ctx: RequestContext) -> list[Budget]:
        return self._budgets.get((BudgetScope.KEY, ctx.key_id), []) + self._budgets.get(
            (BudgetScope.TEAM, ctx.team_id), []
        )

    def _current_spend(self, budget: Budget, now: datetime) -> BudgetSpend:
        spend = self._spend[budget.id]
        if now >= spend.reset_at:
            spend = BudgetSpend(budget.id, budget.period.next_reset(now), Decimal(0))
            self._set_spend(spend)
        return spend

    def _set_spend(self, spend: BudgetSpend) -> None:
        self._spend[spend.budget_id] = spend
        self._dirty[spend.budget_id] = spend

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._flush_interval_s)
            except TimeoutError:
                try:
                    await self._flush()
                except Exception:
                    log.exception("Could not save budget spend. Will retry on the next flush.")

    async def _flush(self) -> None:
        now = self._now()
        for budgets in self._budgets.values():
            for budget in budgets:
                self._current_spend(budget, now)
        if not self._dirty:
            return
        pending, self._dirty = self._dirty, {}
        try:
            await self._store.save(list(pending.values()))
        except Exception:
            # A request can update a budget while the write is in progress.
            self._dirty = pending | self._dirty
            raise
