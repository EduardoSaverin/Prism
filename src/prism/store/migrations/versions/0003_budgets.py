"""Persist budgets and record soft threshold crossings.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # The original SQLite 0001 already had a budgets table. Keep its rows.
    if (
        bind.dialect.name == "sqlite"
        and not op.get_context().as_sql
        and sa.inspect(bind).has_table("budgets")
    ):
        with op.batch_alter_table("budgets") as batch:
            batch.alter_column("limit_usd", type_=sa.Numeric(20, 12))
            batch.alter_column("spent_usd", type_=sa.Numeric(20, 12))
    else:
        op.create_table(
            "budgets",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("scope", sa.String(8), nullable=False),
            sa.Column("scope_id", sa.String(32), nullable=False),
            sa.Column("limit_usd", sa.Numeric(20, 12), nullable=False),
            sa.Column("mode", sa.String(4), nullable=False),
            sa.Column("period", sa.String(8), nullable=False),
            sa.Column("reset_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("spent_usd", sa.Numeric(20, 12), nullable=False),
            sa.CheckConstraint("scope IN ('key', 'team')", name="ck_budget_scope"),
            sa.CheckConstraint("mode IN ('hard', 'soft')", name="ck_budget_mode"),
            sa.CheckConstraint("period IN ('daily', 'weekly', 'monthly')", name="ck_budget_period"),
        )
        op.create_index("ix_budgets_scope", "budgets", ["scope", "scope_id"])
    op.add_column("usage_events", sa.Column("budget_threshold", sa.Integer()))


def downgrade() -> None:
    op.drop_column("usage_events", "budget_threshold")
    op.drop_table("budgets")
