from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from prism.core.context import RequestContext
from prism.core.errors import PrismError
from prism.core.governance.budgets import (
    BudgetMode,
    BudgetPeriod,
    BudgetScope,
    BudgetSpend,
    BudgetTracker,
)
from prism.core.types import ChatMessage, ChatRequest
from prism.main import build_governance, build_usage_recorder, connect_database, create_app
from prism.store.budgets import SqlBudgetStore, create_budget
from prism.store.key_admin import create_key
from prism.store.migrate import upgrade
from prism.store.models import UsageEvent
from prism.store.team_admin import create_team
from tests.scripted import ScriptedAdapter, build, config_for, route


async def test_key_and_team_budgets_track_streams_and_survive_restart(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'budgets.db'}"
    upgrade(url)
    pepper = "p" * 32
    env = {"PRISM_DATABASE_URL": url, "PRISM_KEY_PEPPER": pepper}
    database = connect_database(env)
    await create_team(database.sessions, "ml")
    first = await create_key(database.sessions, pepper=pepper, team_name="ml", name="first")
    second = await create_key(database.sessions, pepper=pepper, team_name="ml", name="second")
    key = await create_budget(
        database.sessions,
        BudgetScope.KEY,
        first.key.key_prefix,
        Decimal("0.000015"),
        BudgetPeriod.DAILY,
        BudgetMode.HARD,
    )
    team = await create_budget(
        database.sessions,
        BudgetScope.TEAM,
        "ml",
        Decimal("0.000018"),
        BudgetPeriod.MONTHLY,
        BudgetMode.SOFT,
    )
    config = config_for(["a"], [route("chat", "a")])
    config["providers"][0]["prices"] = {"a-model": {"input": 1, "output": 1}}

    def app_for(adapter: ScriptedAdapter) -> FastAPI:
        gateway = build(config, {"a": adapter}).gateway
        return create_app(
            gateway.config,
            gateway=gateway,
            governance=build_governance(env, database),
            usage=build_usage_recorder(database),
            database=database,
        )

    async def post(
        client: httpx.AsyncClient, token: str, rid: str, *, stream: bool = False
    ) -> httpx.Response:
        return await client.post(
            "/v1/chat/completions",
            json={
                "model": "chat",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": stream,
            },
            headers={"Authorization": f"Bearer {token}", "x-request-id": rid},
        )

    app = app_for(ScriptedAdapter("a"))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await post(client, first.secret, "stream", stream=True)
        assert response.status_code == 200
        assert "data: [DONE]" in response.text
        denied = await post(client, first.secret, "denied", stream=True)
        assert denied.status_code == 429
        assert denied.json()["error"]["code"] == "budget_exceeded"
        assert int(denied.headers["Retry-After"]) > 0
        assert (await post(client, second.secret, "regular")).status_code == 200

    persisted = {
        budget.id: spend for budget, spend in await SqlBudgetStore(database.sessions).load()
    }
    assert persisted[key.id].spent_usd == Decimal("0.000015")
    assert persisted[team.id].spent_usd == Decimal("0.000030")
    assert persisted[key.id].reset_at.tzinfo == UTC
    async with database.sessions() as session:
        events = {event.request_id: event for event in await session.scalars(select(UsageEvent))}
        assert events["stream"].budget_threshold == 80
        assert events["regular"].budget_threshold == 100
        assert events["denied"].cost_usd == 0
        assert events["denied"].attempts == 0

    restarted = app_for(ScriptedAdapter("a"))
    async with (
        restarted.router.lifespan_context(restarted),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client,
    ):
        assert (await post(client, first.secret, "still-denied")).status_code == 429


async def test_expired_team_budget_resets_and_applies_to_every_team_key(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'reset.db'}"
    upgrade(url)
    database = connect_database({"PRISM_DATABASE_URL": url})
    team = await create_team(database.sessions, "ml")
    row = await create_budget(
        database.sessions,
        BudgetScope.TEAM,
        "ml",
        Decimal("1"),
        BudgetPeriod.WEEKLY,
        BudgetMode.HARD,
    )
    store = SqlBudgetStore(database.sessions)
    reset = datetime(2026, 11, 23, tzinfo=UTC)
    await store.save([BudgetSpend(row.id, reset, Decimal("1"))])
    service = BudgetTracker(store, now=lambda: reset)
    request = ChatRequest(model="chat", messages=[ChatMessage(role="user", content="hi")])
    first = RequestContext(request, "chat", key_id="one", team_id=team.id, cost_usd=Decimal("1"))
    second = RequestContext(request, "chat", key_id="two", team_id=team.id)
    await service.start()
    try:
        service.check(first)
        service.charge(first)
        with pytest.raises(PrismError, match="team budget"):
            service.check(second)
    finally:
        await service.aclose()
    [(_, saved)] = await store.load()
    assert saved.reset_at == datetime(2026, 11, 30, tzinfo=UTC)
    assert saved.spent_usd == Decimal("1")
    await database.aclose()
