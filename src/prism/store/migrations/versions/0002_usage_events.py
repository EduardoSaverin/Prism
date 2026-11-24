"""usage events

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    schema = (
        sa.inspect(bind) if bind.dialect.name == "sqlite" and not op.get_context().as_sql else None
    )
    if schema is not None and schema.has_table("usage_events"):
        # Early SQLite installs used a larger schema under the same 0001 revision.
        if "updated_at" not in {column["name"] for column in schema.get_columns("virtual_keys")}:
            _upgrade_legacy_keys()
        _upgrade_legacy_usage()
    else:
        _create_usage_table()

    op.create_index("ix_usage_events_ts", "usage_events", ["ts"], unique=False)
    op.create_index("ix_usage_events_team_id_ts", "usage_events", ["team_id", "ts"], unique=False)
    op.create_index("ix_usage_events_key_id_ts", "usage_events", ["key_id", "ts"], unique=False)


def _create_usage_table() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("route", sa.String(length=255), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=True),
        sa.Column("team_id", sa.String(length=32), nullable=True),
        sa.Column("provider", sa.String(length=255), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=20, scale=12), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def _upgrade_legacy_keys() -> None:
    op.add_column("virtual_keys", sa.Column("name", sa.String(128)))
    op.add_column("virtual_keys", sa.Column("created_at", sa.DateTime(timezone=True)))
    op.add_column("virtual_keys", sa.Column("updated_at", sa.DateTime(timezone=True)))
    # The old schema had no labels or timestamps. Use the existing display prefix and UTC now.
    op.execute(
        "UPDATE virtual_keys SET name = key_prefix, "
        "created_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP"
    )
    with op.batch_alter_table("virtual_keys") as batch:
        batch.alter_column("key_prefix", type_=sa.String(32))
        batch.alter_column("name", nullable=False)
        batch.alter_column("created_at", nullable=False)
        batch.alter_column("updated_at", nullable=False)
        batch.create_index("ix_virtual_keys_updated_at", ["updated_at"])


def _upgrade_legacy_usage() -> None:
    op.add_column("usage_events", sa.Column("team_id", sa.String(32)))
    op.add_column("usage_events", sa.Column("attempts", sa.Integer()))
    # An old key may have been deleted, and the old schema never recorded attempts.
    op.execute(
        "UPDATE usage_events SET team_id = "
        "(SELECT team_id FROM virtual_keys WHERE virtual_keys.id = usage_events.key_id), "
        "attempts = 0"
    )
    with op.batch_alter_table("usage_events") as batch:
        batch.alter_column("id", type_=sa.String(32), autoincrement=False)
        batch.alter_column("request_id", type_=sa.String(128))
        batch.alter_column("route", type_=sa.String(255))
        batch.alter_column("provider", type_=sa.String(255), nullable=True)
        batch.alter_column("model", type_=sa.String(255), nullable=True)
        batch.alter_column("cost_usd", type_=sa.Numeric(20, 12))
        batch.alter_column("attempts", nullable=False)
        batch.drop_index("ix_usage_ts")
        batch.drop_index("ix_usage_key_ts")


def downgrade() -> None:
    op.drop_index("ix_usage_events_key_id_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_team_id_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_ts", table_name="usage_events")
    op.drop_table("usage_events")
