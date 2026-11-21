"""The web app."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from prism import __version__
from prism.api import openai as openai_api
from prism.config import PrismConfig, load_config
from prism.core.errors import ErrorKind, PrismError
from prism.core.gateway import Gateway
from prism.core.governance.keys import Authenticator, InMemoryKeyCache, KeyCache
from prism.providers.registry import build_gateway

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


def build_governance(environ: Mapping[str, str]) -> Governance:
    """Connect to the database (and Redis, if configured) for key lookups.

    Raises ValueError when PRISM_KEY_PEPPER is missing or too short.
    """
    from prism.core.governance.keys import check_pepper
    from prism.store.db import DEFAULT_DATABASE_URL, make_engine, make_session_factory
    from prism.store.keys import KeyChangeWatcher, SqlKeyLookup

    pepper = check_pepper(environ.get("PRISM_KEY_PEPPER"))
    engine = make_engine(environ.get("PRISM_DATABASE_URL") or DEFAULT_DATABASE_URL)
    sessions = make_session_factory(engine)
    services: list[Service] = []

    cache: KeyCache
    redis_url = environ.get("PRISM_REDIS_URL")
    if redis_url:
        from prism.store.redis_cache import RedisKeyCache, connect

        client = connect(redis_url)
        cache = RedisKeyCache(client)
        services.append(_Closer(client.aclose))
    else:
        cache = InMemoryKeyCache()

    services.append(KeyChangeWatcher(sessions, cache))
    services.append(_Closer(engine.dispose))
    return Governance(Authenticator(SqlKeyLookup(sessions), cache, pepper), services)


@dataclass
class _Closer:
    """Runs one async cleanup function when the app stops."""

    close: Callable[[], Awaitable[None]]

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        await self.close()


def create_app(
    config: PrismConfig | None = None,
    *,
    gateway: Gateway | None = None,
    governance: Governance | None = None,
    auth_disabled: bool = False,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the app. Pass a config to load routes and providers.

    Every /v1 request needs a virtual key, checked by `governance`. With no
    governance, /v1 refuses every request, unless `auth_disabled` is set.
    """
    if gateway is None and config is not None:
        gateway = build_gateway(config, os.environ if environ is None else environ)
    services = governance.services if governance else ()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for service in services:
            await service.start()
        try:
            yield
        finally:
            for service in reversed(services):
                await service.aclose()
            if app.state.gateway is not None:
                await app.state.gateway.aclose()

    app = FastAPI(title="Prism", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.gateway = gateway
    app.state.authenticator = governance.authenticator if governance else None
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
    cache when it is set.
    """
    path = os.environ.get("PRISM_CONFIG")
    return create_app(load_config(path) if path else None, governance=build_governance(os.environ))
