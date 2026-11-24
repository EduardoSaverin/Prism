"""Alembic environment. Migrations run with a normal (blocking) connection."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from prism.store.db import DEFAULT_DATABASE_URL
from prism.store.migrate import to_sync_url
from prism.store.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return to_sync_url(os.environ.get("PRISM_DATABASE_URL", DEFAULT_DATABASE_URL))


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            # SQLite's legacy transaction mode does not begin a transaction for DDL.
            # Keep schema changes, data copies, and the revision update atomic.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
