"""Teams in a real (SQLite) database."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from prism.store.admin import AdminError, Sessions
from prism.store.db import make_engine, make_session_factory
from prism.store.migrate import upgrade
from prism.store.team_admin import TeamNotFoundError, create_team, list_teams, team_by_name


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[Sessions]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'teams.db'}"
    upgrade(url)
    engine = make_engine(url)
    yield make_session_factory(engine)
    await engine.dispose()


async def test_teams_are_unique_and_listed_by_name(sessions: Sessions) -> None:
    await create_team(sessions, "ml")
    await create_team(sessions, "data")
    with pytest.raises(AdminError, match="already exists"):
        await create_team(sessions, "ml")
    assert [t.name for t in await list_teams(sessions)] == ["data", "ml"]


async def test_a_team_is_found_by_name(sessions: Sessions) -> None:
    created = await create_team(sessions, "ml")
    async with sessions() as session:
        assert (await team_by_name(session, "ml")).id == created.id
        with pytest.raises(TeamNotFoundError, match="no team named 'ghost'") as info:
            await team_by_name(session, "ghost")
    assert info.value.name == "ghost"


@pytest.mark.parametrize("name", ["", "  ", "my team"])
async def test_an_invalid_name_creates_no_team(sessions: Sessions, name: str) -> None:
    with pytest.raises(AdminError, match="team name"):
        await create_team(sessions, name)
    assert await list_teams(sessions) == []
