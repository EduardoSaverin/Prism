"""The database schema.

Only tables that code uses are defined here. Each Phase 2 slice adds the
tables it needs (budgets, ...) together with that code. See ADR 0011.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from prism.core.governance.keys import MAX_KEY_NAME_LENGTH
from prism.core.governance.teams import MAX_TEAM_NAME_LENGTH


def new_uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(MAX_TEAM_NAME_LENGTH), unique=True)


class VirtualKey(Base):
    __tablename__ = "virtual_keys"
    __table_args__ = (Index("ix_virtual_keys_updated_at", "updated_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(MAX_KEY_NAME_LENGTH))
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    allowed_routes: Mapped[list[str]] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Set on every change. Running instances poll it to evict changed keys from their cache.
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageEvent(Base):
    """One finished request: what it used, what it cost and how it ended (ADR 0010).

    `key_id` and `team_id` are plain columns, not foreign keys, so usage history
    stays when a key or team is deleted.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_ts", "ts"),
        Index("ix_usage_events_team_id_ts", "team_id", "ts"),
        Index("ix_usage_events_key_id_ts", "key_id", "ts"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[str] = mapped_column(String(128))
    route: Mapped[str] = mapped_column(String(255))
    key_id: Mapped[str | None] = mapped_column(String(32))
    team_id: Mapped[str | None] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(255))
    model: Mapped[str | None] = mapped_column(String(255))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    # US dollars. 12 decimal places hold a single token at the cheapest prices.
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(20, 12, asdecimal=True))
    latency_ms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    attempts: Mapped[int] = mapped_column(Integer)
    budget_threshold: Mapped[int | None] = mapped_column(Integer)


class BudgetRow(Base):
    __tablename__ = "budgets"
    __table_args__ = (
        Index("ix_budgets_scope", "scope", "scope_id"),
        CheckConstraint("scope IN ('key', 'team')", name="ck_budget_scope"),
        CheckConstraint("mode IN ('hard', 'soft')", name="ck_budget_mode"),
        CheckConstraint("period IN ('daily', 'weekly', 'monthly')", name="ck_budget_period"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    scope: Mapped[str] = mapped_column(String(8))
    scope_id: Mapped[str] = mapped_column(String(32))
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    mode: Mapped[str] = mapped_column(String(4))
    period: Mapped[str] = mapped_column(String(8))
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(20, 12))


class AppliedBudgetBatch(Base):
    __tablename__ = "applied_budget_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
