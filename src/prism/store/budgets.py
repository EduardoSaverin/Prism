"""Budget setup and persistence of in-memory spend snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import case, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from prism.core.governance.budgets import (
    Budget,
    BudgetBatch,
    BudgetMode,
    BudgetPeriod,
    BudgetScope,
    BudgetSpend,
)
from prism.store.admin import AdminError, Sessions
from prism.store.key_admin import key_by_id_or_prefix
from prism.store.models import AppliedBudgetBatch, BudgetRow
from prism.store.team_admin import team_by_name


class SqlBudgetStore:
    def __init__(self, sessions: Sessions) -> None:
        self._sessions = sessions

    async def load(self) -> list[tuple[Budget, BudgetSpend]]:
        async with self._sessions() as session:
            rows = await session.scalars(select(BudgetRow).order_by(BudgetRow.scope, BudgetRow.id))
            return [
                (
                    Budget(
                        row.id,
                        BudgetScope(row.scope),
                        row.scope_id,
                        row.limit_usd,
                        BudgetMode(row.mode),
                        BudgetPeriod(row.period),
                    ),
                    BudgetSpend(
                        row.id,
                        row.reset_at.replace(tzinfo=UTC)
                        if not row.reset_at.tzinfo
                        else row.reset_at,
                        row.spent_usd,
                    ),
                )
                for row in rows
            ]

    async def save(self, batch: BudgetBatch) -> None:
        async with self._sessions() as session, session.begin():
            insert = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
            receipt = (
                insert(AppliedBudgetBatch)
                .values(id=batch.id)
                .on_conflict_do_nothing(index_elements=[AppliedBudgetBatch.id])
                .returning(AppliedBudgetBatch.id)
            )
            if await session.scalar(receipt) is None:
                return
            for delta in batch.deltas:
                # Advance an expired period once. A late prior-period write cannot
                # roll the row backwards or add its charge to the new period.
                await session.execute(
                    update(BudgetRow)
                    .where(BudgetRow.id == delta.budget_id, BudgetRow.reset_at <= delta.reset_at)
                    .values(
                        spent_usd=case(
                            (
                                BudgetRow.reset_at == delta.reset_at,
                                BudgetRow.spent_usd + delta.amount,
                            ),
                            else_=delta.amount,
                        ),
                        reset_at=delta.reset_at,
                    )
                )


async def create_budget(
    sessions: Sessions,
    scope: BudgetScope,
    target: str,
    limit_usd: Decimal,
    period: BudgetPeriod,
    mode: BudgetMode,
) -> BudgetRow:
    if not limit_usd.is_finite() or not 0 < limit_usd < 100_000_000:
        raise AdminError("The budget limit must be greater than $0 and below $100,000,000.")
    if limit_usd != limit_usd.quantize(Decimal("0.000000000001")):
        raise AdminError("The budget limit can have at most 12 decimal places.")
    async with sessions() as session, session.begin():
        if scope is BudgetScope.KEY:
            scope_id = (await key_by_id_or_prefix(session, target)).id
        else:
            scope_id = (await team_by_name(session, target)).id
        row = BudgetRow(
            scope=scope.value,
            scope_id=scope_id,
            limit_usd=limit_usd,
            mode=mode.value,
            period=period.value,
            reset_at=period.next_reset(datetime.now(UTC)),
            spent_usd=Decimal(0),
        )
        session.add(row)
    return row
