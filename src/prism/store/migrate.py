"""Run database migrations from code."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_SYNC_DRIVERS = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql+psycopg",
}


def to_sync_url(url: str) -> str:
    for async_driver, sync_driver in _SYNC_DRIVERS.items():
        if url.startswith(async_driver):
            return sync_driver + url[len(async_driver) :]
    return url


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", to_sync_url(url).replace("%", "%%"))
    return config


def upgrade(url: str, revision: str = "head") -> None:
    """Bring the database up to date."""
    command.upgrade(alembic_config(url), revision)
