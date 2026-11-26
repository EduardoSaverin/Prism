"""Shared hard-budget admission and retryable spend settlement."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

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


@dataclass(frozen=True)
class BudgetDelta:
    budget_id: str
    reset_at: datetime
    amount: Decimal


@dataclass(frozen=True)
class BudgetBatch:
    id: str
    deltas: tuple[BudgetDelta, ...]


class BudgetStore(Protocol):
    async def load(self) -> list[tuple[Budget, BudgetSpend]]: ...

    async def exhausted(self, ids: list[str], now: datetime) -> tuple[str, datetime] | None: ...

    async def save(self, batch: BudgetBatch) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BudgetTracker:
    """Check shared hard limits and persist completed charges."""

    def __init__(self, store: BudgetStore, *, now: Now = _utcnow, flush_interval_s: float = 5):
        self._store = store
        self._now = now
        self._flush_interval_s = flush_interval_s
        self._budgets: dict[tuple[BudgetScope, str | None], list[Budget]] = {}
        self._spend: dict[str, BudgetSpend] = {}
        self._pending: dict[tuple[str, datetime], Decimal] = {}
        self._retry: BudgetBatch | None = None
        self._flush_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._budgets.clear()
        self._spend.clear()
        self._pending.clear()
        self._retry = None
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

    async def admit(self, ctx: RequestContext) -> None:
        budgets = self._applicable(ctx)
        now = self._now()
        self._check_local(budgets, now)
        ids = [budget.id for budget in budgets if budget.mode is BudgetMode.HARD]
        if not ids:
            return
        try:
            exhausted = await self._store.exhausted(ids, now)
        except Exception as exc:
            log.exception("Could not check shared budget spend")
            raise PrismError(ErrorKind.BUDGET_UNAVAILABLE, "Budget check unavailable.") from exc
        if exhausted is not None:
            scope, reset_at = exhausted
            raise PrismError(
                ErrorKind.BUDGET_EXCEEDED,
                f"The {scope} budget has been reached.",
                retry_after_s=(reset_at - now).total_seconds(),
            )

    async def settle(self, ctx: RequestContext) -> int | None:
        if not ctx.cost_usd:
            return None
        budgets = self._applicable(ctx)
        threshold = self._charge_local(budgets, ctx.cost_usd)
        if budgets:
            try:
                async with self._flush_lock:
                    await self._write_pending()
            except Exception as exc:
                log.exception("Could not settle budget spend; retained for retry")
                raise PrismError(
                    ErrorKind.BUDGET_UNAVAILABLE, "Budget settlement unavailable."
                ) from exc
        return threshold

    def _check_local(self, budgets: list[Budget], now: datetime) -> None:
        for budget in budgets:
            spend = self._current_spend(budget, now)
            if budget.mode is BudgetMode.HARD and spend.spent_usd >= budget.limit_usd:
                # TODO: Consider an opt-in route fallback restricted to explicitly zero-priced
                # targets when a hard budget is exhausted. Missing prices must not qualify.
                raise PrismError(
                    ErrorKind.BUDGET_EXCEEDED,
                    f"The {budget.scope} budget has been reached.",
                    retry_after_s=(spend.reset_at - now).total_seconds(),
                )

    def _charge_local(self, budgets: list[Budget], cost: Decimal) -> int | None:
        if cost <= 0:
            return None
        now = self._now()
        threshold: int | None = None
        for budget in budgets:
            previous = self._current_spend(budget, now)
            spent = previous.spent_usd + cost
            self._spend[budget.id] = BudgetSpend(budget.id, previous.reset_at, spent)
            key = (budget.id, previous.reset_at)
            self._pending[key] = self._pending.get(key, Decimal(0)) + cost
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
            self._spend[budget.id] = spend
            self._pending.setdefault((budget.id, spend.reset_at), Decimal(0))
        return spend

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
        async with self._flush_lock:
            await self._write_pending()

    async def _write_pending(self) -> None:
        # Keep an unacknowledged batch immutable. Charges arriving during its write
        # accumulate separately, even if the database committed before a disconnect.
        if self._retry is not None:
            await self._store.save(self._retry)
            self._retry = None
        if self._pending:
            deltas = tuple(
                BudgetDelta(budget_id, reset_at, amount)
                for (budget_id, reset_at), amount in sorted(self._pending.items())
            )
            self._retry = BudgetBatch(uuid4().hex, deltas)
            self._pending.clear()
            await self._store.save(self._retry)
            self._retry = None
