"""Record applied budget batches in the same transaction as their increments."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # SQLite compares datetime strings lexically. Legacy CURRENT_TIMESTAMP
        # values omit the fractional part that SQLAlchemy includes when binding.
        op.execute(
            "UPDATE budgets SET reset_at = reset_at || '.000000' WHERE length(reset_at) = 19"
        )
    op.create_table("applied_budget_batches", sa.Column("id", sa.String(32), primary_key=True))


def downgrade() -> None:
    op.drop_table("applied_budget_batches")
