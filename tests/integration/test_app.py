from pathlib import Path

import httpx
import pytest

from prism import __version__
from prism.config import load_config
from prism.main import app_from_env, create_app


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    )


async def test_healthz_says_ok() -> None:
    async with client_for(create_app()) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_without_a_config() -> None:
    async with client_for(create_app()) as client:
        body = (await client.get("/readyz")).json()
    assert body == {
        "status": "ready",
        "version": __version__,
        "config_loaded": False,
        "providers": 0,
        "routes": 0,
    }


async def test_readyz_with_a_config(example_config_path: Path) -> None:
    app = create_app(load_config(example_config_path))
    async with client_for(app) as client:
        body = (await client.get("/readyz")).json()
    assert body["config_loaded"] is True
    assert (body["providers"], body["routes"]) == (3, 1)


async def test_app_from_env_reads_prism_config(
    example_config_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRISM_KEY_PEPPER", "p" * 32)
    monkeypatch.setenv("PRISM_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'env.db'}")
    monkeypatch.delenv("PRISM_REDIS_URL", raising=False)
    monkeypatch.setenv("PRISM_CONFIG", str(example_config_path))
    async with client_for(app_from_env()) as client:
        assert (await client.get("/readyz")).json()["routes"] == 1
    monkeypatch.delenv("PRISM_CONFIG")
    async with client_for(app_from_env()) as client:
        assert (await client.get("/readyz")).json()["config_loaded"] is False


async def test_the_api_describes_itself() -> None:
    async with client_for(create_app()) as client:
        spec = (await client.get("/openapi.json")).json()
    assert {"/healthz", "/readyz"} <= set(spec["paths"])


def test_app_from_env_needs_a_pepper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRISM_KEY_PEPPER", raising=False)
    with pytest.raises(ValueError, match="PRISM_KEY_PEPPER"):
        app_from_env()
