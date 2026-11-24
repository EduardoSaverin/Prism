"""The web app."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from prism import __version__
from prism.api import openai as openai_api
from prism.config import PrismConfig, load_config
from prism.core.errors import ErrorKind, PrismError
from prism.core.gateway import Gateway
from prism.core.governance.budgets import BudgetTracker
from prism.core.governance.keys import Authenticator, InMemoryKeyCache, KeyCache
from prism.core.usage.batching import BatchingUsageRecorder
from prism.providers.registry import build_gateway

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from prism.store.admin import Sessions

log = logging.getLogger(__name__)


class Service(Protocol):
    """Something that runs in the background while the app is up."""

    async def start(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass
class Governance:
    """Virtual key authentication and the background services it needs."""

    authenticator: Authenticator
    services: Sequence[Service] = field(default_factory=lambda: [])
    budgets: BudgetTracker | None = None


@dataclass
class Database:
    """One connection pool, shared by key lookups and usage writes. Closed last."""

    engine: AsyncEngine
    sessions: Sessions

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        await self.engine.dispose()


def connect_database(environ: Mapping[str, str]) -> Database:
    """Open the database named by PRISM_DATABASE_URL (default: SQLite in ./prism.db)."""
    from prism.store.db import DEFAULT_DATABASE_URL, make_engine, make_session_factory

    engine = make_engine(environ.get("PRISM_DATABASE_URL") or DEFAULT_DATABASE_URL)
    return Database(engine, make_session_factory(engine))


def build_governance(environ: Mapping[str, str], database: Database) -> Governance:
    """Look virtual keys up in the database, cached in memory or in Redis if configured.

    Raises ValueError when PRISM_KEY_PEPPER is missing or too short.
    """
    from prism.core.governance.keys import check_pepper
    from prism.core.governance.watcher import KeyChangeWatcher
    from prism.store.budgets import SqlBudgetStore
    from prism.store.keys import SqlKeyChangeSource, SqlKeyLookup

    pepper = check_pepper(environ.get("PRISM_KEY_PEPPER"))
    sessions = database.sessions
    services: list[Service] = []

    # Redis or InMemory Cache
    cache: KeyCache
    redis_url = environ.get("PRISM_REDIS_URL")
    if redis_url:
        from prism.store.redis_cache import RedisKeyCache, connect

        cache = RedisKeyCache(connect(redis_url))
        services.append(cache)
    else:
        cache = InMemoryKeyCache()

    # Started and stopped by create_app's lifespan, like every Service.
    services.append(KeyChangeWatcher(SqlKeyChangeSource(sessions), cache))
    budgets = BudgetTracker(SqlBudgetStore(sessions))
    services.append(budgets)
    return Governance(Authenticator(SqlKeyLookup(sessions), cache, pepper), services, budgets)


def build_usage_recorder(database: Database) -> BatchingUsageRecorder:
    """Write usage records to the usage_events table in batches."""
    from prism.store.usage import SqlUsageSink

    return BatchingUsageRecorder(SqlUsageSink(database.sessions))


def create_app(
    config: PrismConfig | None = None,
    *,
    gateway: Gateway | None = None,
    governance: Governance | None = None,
    usage: BatchingUsageRecorder | None = None,
    database: Service | None = None,
    auth_disabled: bool = False,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the app. Pass a config to load routes and providers.

    Every /v1 request needs a virtual key, checked by `governance`. With no
    governance, /v1 refuses every request, unless `auth_disabled` is set.
    With a `usage` recorder, every /v1/chat/completions request leaves a usage record.
    """
    if gateway is None and config is not None:
        gateway = build_gateway(config, os.environ if environ is None else environ)
    # Started in this order and stopped in reverse: the usage recorder writes its
    # last batch before the database it writes to is closed.
    services: list[Service] = [
        *([database] if database else []),
        *(governance.services if governance else []),
        *([usage] if usage else []),
    ]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as cleanup:
            if gateway is not None:
                cleanup.push_async_callback(gateway.aclose)
            for service in services:
                await service.start()
                cleanup.push_async_callback(service.aclose)
            yield

    app = FastAPI(title="Prism", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.gateway = gateway
    app.state.authenticator = governance.authenticator if governance else None
    app.state.budgets = governance.budgets if governance else None
    app.state.usage = usage
    app.state.auth_disabled = auth_disabled

    @app.exception_handler(PrismError)
    async def _prism_error(request: Request, exc: PrismError) -> JSONResponse:
        headers = {**exc.headers(), "x-prism-request-id": openai_api.request_id(request)}
        return JSONResponse(exc.to_openai_error(), status_code=exc.http_status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc'] if p != 'body')}: {error['msg']}"
            for error in exc.errors()
        )
        error = PrismError(ErrorKind.INVALID_REQUEST, problems or "The request body is invalid.")
        return await _prism_error(request, error)

    @app.get("/healthz", tags=["Health"], summary="Liveness check")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["Health"], summary="Readiness check")
    async def readyz() -> dict[str, object]:
        loaded: PrismConfig | None = app.state.config
        return {
            "status": "ready",
            "version": __version__,
            "config_loaded": loaded is not None,
            "providers": len(loaded.providers) if loaded else 0,
            "routes": len(loaded.routes) if loaded else 0,
        }

    app.include_router(openai_api.router)
    return app


def app_from_env() -> FastAPI:
    """Build the app from environment variables (for uvicorn --factory).

    PRISM_CONFIG names the config file. Virtual keys are checked against
    PRISM_DATABASE_URL using PRISM_KEY_PEPPER, with PRISM_REDIS_URL as a shared
    cache when it is set. Usage records go to the same database.
    """
    path = os.environ.get("PRISM_CONFIG")
    config = load_config(path) if path else None
    database = connect_database(os.environ)
    return create_app(
        config,
        governance=build_governance(os.environ, database),
        usage=build_usage_recorder(database),
        database=database,
    )
