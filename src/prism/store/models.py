"""The database schema.

Only tables that code uses are defined here. Each Phase 2 slice adds the
tables it needs (usage events, budgets, ...) together with that code. See ADR 0011.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
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
