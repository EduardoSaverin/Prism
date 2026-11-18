"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-18 07:15:43.992760
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=256), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=8), nullable=False),
        sa.Column("scope_id", sa.String(length=32), nullable=False),
        sa.Column("limit_usd", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("mode", sa.String(length=4), nullable=False),
        sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spent_usd", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.CheckConstraint("mode IN ('hard', 'soft')", name="ck_budget_mode"),
        sa.CheckConstraint("period IN ('daily', 'weekly', 'monthly')", name="ck_budget_period"),
        sa.CheckConstraint("scope IN ('key', 'team')", name="ck_budget_scope"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("budgets", schema=None) as batch_op:
        batch_op.create_index("ix_budgets_scope", ["scope", "scope_id"], unique=False)

    op.create_table(
        "plugin_configs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=160), nullable=False),
        sa.Column("plugin", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "providers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "routes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("policy", sa.String(length=32), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "teams",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "usage_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=True),
        sa.Column("route", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.create_index("ix_usage_key_ts", ["key_id", "ts"], unique=False)
        batch_op.create_index("ix_usage_route_ts", ["route", "ts"], unique=False)
        batch_op.create_index("ix_usage_ts", ["ts"], unique=False)

    op.create_table(
        "provider_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("secret_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("daily_quota", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled', 'exhausted')", name="ck_key_status"),
        sa.ForeignKeyConstraint(["provider_id"], ["providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "route_targets",
        sa.Column("route_id", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("key_pool", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["providers.id"],
        ),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("route_id", "position"),
    )
    op.create_table(
        "virtual_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("allowed_routes", sa.JSON(), nullable=False),
        sa.Column("rpm", sa.Integer(), nullable=True),
        sa.Column("tpm", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )


def downgrade() -> None:
    op.drop_table("virtual_keys")
    op.drop_table("route_targets")
    op.drop_table("provider_keys")
    with op.batch_alter_table("usage_events", schema=None) as batch_op:
        batch_op.drop_index("ix_usage_ts")
        batch_op.drop_index("ix_usage_route_ts")
        batch_op.drop_index("ix_usage_key_ts")

    op.drop_table("usage_events")
    op.drop_table("teams")
    op.drop_table("routes")
    op.drop_table("providers")
    op.drop_table("plugin_configs")
    with op.batch_alter_table("budgets", schema=None) as batch_op:
        batch_op.drop_index("ix_budgets_scope")

    op.drop_table("budgets")
    op.drop_table("audit_log")
