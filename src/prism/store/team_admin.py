"""Managing teams in the database: create, list and find by name. Used by the CLI.

A team owns virtual keys and can have spend budgets.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from prism.core.governance.teams import check_team_name
from prism.store.admin import AdminError, Sessions
from prism.store.models import Team


class TeamNotFoundError(AdminError):
    """No team has this name. Callers can use `name` to suggest how to create it."""

    def __init__(self, name: str) -> None:
        super().__init__(f"There is no team named '{name}'.")
        self.name = name


async def create_team(sessions: Sessions, name: str) -> Team:
    try:
        check_team_name(name)
    except ValueError as exc:
        raise AdminError(str(exc)) from exc
    team = Team(name=name)
    try:
        async with sessions() as session, session.begin():
            session.add(team)
    except IntegrityError as exc:
        raise AdminError(f"A team named '{name}' already exists.") from exc
    return team


async def list_teams(sessions: Sessions) -> list[Team]:
    async with sessions() as session:
        return list(await session.scalars(select(Team).order_by(Team.name)))


async def team_by_name(session: AsyncSession, name: str) -> Team:
    """The team with this name, or TeamNotFoundError if there is none."""
    team: Team | None = await session.scalar(select(Team).where(Team.name == name))
    if team is None:
        raise TeamNotFoundError(name)
    return team
