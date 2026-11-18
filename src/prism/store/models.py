"""The database schema."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BigIntPk = BigInteger().with_variant(Integer, "sqlite")


def new_uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(512), default=None)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ProviderKey(Base):
    __tablename__ = "provider_keys"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled', 'exhausted')", name="ck_key_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    daily_quota: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(16), default="active")


class Route(Base):
    __tablename__ = "routes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    policy: Mapped[str] = mapped_column(String(32), default="priority")
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class RouteTarget(Base):
    __tablename__ = "route_targets"

    route_id: Mapped[str] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id"))
    model: Mapped[str] = mapped_column(String(256))
    key_pool: Mapped[list[str]] = mapped_column(JSON, default=list)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True)


class VirtualKey(Base):
    __tablename__ = "virtual_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16))
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"))
    allowed_routes: Mapped[list[str]] = mapped_column(JSON, default=list)
    rpm: Mapped[int | None] = mapped_column(Integer, default=None)
    tpm: Mapped[int | None] = mapped_column(Integer, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (
        CheckConstraint("scope IN ('key', 'team')", name="ck_budget_scope"),
        CheckConstraint("period IN ('daily', 'weekly', 'monthly')", name="ck_budget_period"),
        CheckConstraint("mode IN ('hard', 'soft')", name="ck_budget_mode"),
        Index("ix_budgets_scope", "scope", "scope_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    scope: Mapped[str] = mapped_column(String(8))
    scope_id: Mapped[str] = mapped_column(String(32))
    limit_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    period: Mapped[str] = mapped_column(String(8))
    mode: Mapped[str] = mapped_column(String(4), default="hard")
    reset_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    spent_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))


class UsageEvent(Base):
    """One row per request. Written in batches, never on the request path."""

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_ts", "ts"),
        Index("ix_usage_key_ts", "key_id", "ts"),
        Index("ix_usage_route_ts", "route", "ts"),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    request_id: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str | None] = mapped_column(String(32), default=None)
    route: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(256))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal(0))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)


class PluginConfig(Base):
    __tablename__ = "plugin_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_uuid)
    scope: Mapped[str] = mapped_column(String(160))
    plugin: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer, default=0)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    """Every change made through the admin API."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(256))
