"""The web app."""

from __future__ import annotations

import os

from fastapi import FastAPI

from prism import __version__
from prism.config import PrismConfig, load_config


def create_app(config: PrismConfig | None = None) -> FastAPI:
    """Build the app. Pass a config to load routes and providers."""
    app = FastAPI(title="Prism", version=__version__)
    app.state.config = config

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

    return app


def app_from_env() -> FastAPI:
    """Build the app from the PRISM_CONFIG environment variable (for uvicorn --factory)."""
    path = os.environ.get("PRISM_CONFIG")
    return create_app(load_config(path) if path else None)
