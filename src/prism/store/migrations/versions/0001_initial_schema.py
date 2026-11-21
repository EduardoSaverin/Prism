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
        "teams",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "virtual_keys",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("team_id", sa.String(length=32), nullable=False),
        sa.Column("allowed_routes", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    with op.batch_alter_table("virtual_keys", schema=None) as batch_op:
        batch_op.create_index("ix_virtual_keys_updated_at", ["updated_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("virtual_keys", schema=None) as batch_op:
        batch_op.drop_index("ix_virtual_keys_updated_at")

    op.drop_table("virtual_keys")
    op.drop_table("teams")
