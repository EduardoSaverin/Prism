"""What the store's management functions share."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]


class AdminError(Exception):
    """A management action that cannot be done, with a message for the user."""
