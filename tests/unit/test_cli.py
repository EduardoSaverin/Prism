from pathlib import Path

import pytest
from typer.testing import CliRunner

from prism import __version__
from prism.cli import EXIT_BAD_CONFIG, EXIT_MISSING_ENV, EXIT_OK, app
from prism.core.governance.keys import DISPLAY_PREFIX_LENGTH
from prism.store.migrate import upgrade

KEY_VARS = ["GROQ_KEY_1", "GROQ_KEY_2", "GEMINI_KEY_1"]

runner = CliRunner()


@pytest.fixture
def no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)


def test_check_config_accepts_the_example(example_config_path: Path, no_keys: None) -> None:
    result = runner.invoke(app, ["check-config", str(example_config_path)])
    assert result.exit_code == EXIT_OK
    assert "3 providers, 1 routes" in result.stdout
    assert "GROQ_KEY_1" in result.stdout


def test_check_config_can_require_the_keys(example_config_path: Path, no_keys: None) -> None:
    result = runner.invoke(app, ["check-config", str(example_config_path), "--require-env"])
    assert result.exit_code == EXIT_MISSING_ENV


def test_check_config_passes_when_keys_are_set(
    example_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in KEY_VARS:
        monkeypatch.setenv(name, "x")
    result = runner.invoke(app, ["check-config", str(example_config_path), "--require-env"])
    assert result.exit_code == EXIT_OK


def test_check_config_reports_a_broken_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("providers: []\nroutes: []\n", encoding="utf-8")
    result = runner.invoke(app, ["check-config", str(bad)])
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "Config is not valid" in result.stderr


def test_serve_refuses_to_start_with_a_broken_config(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("nothing: here\n", encoding="utf-8")
    result = runner.invoke(app, ["serve", "--config", str(bad)])
    assert result.exit_code == EXIT_BAD_CONFIG


def test_db_upgrade_creates_the_tables(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}"
    result = runner.invoke(app, ["db-upgrade", "--url", url])
    assert result.exit_code == EXIT_OK
    assert "up to date" in result.stdout
    assert (tmp_path / "cli.db").exists()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == EXIT_OK
    assert __version__ in result.stdout


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "check-config" in result.stdout


def test_serve_refuses_a_policy_that_is_not_available_yet(tmp_path: Path) -> None:
    config = tmp_path / "cost.yaml"
    config.write_text(
        "providers:\n  - id: local\n    kind: ollama\n"
        "routes:\n  - name: chat\n    policy: cost\n"
        "    targets:\n      - provider: local\n        model: m\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["serve", "--config", str(config), "--no-auth"])
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "not available yet" in result.stderr


# ---------- Authentication and key management ----------

PEPPER = "p" * 32


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite+aiosqlite:///{tmp_path / 'keys.db'}"
    monkeypatch.setenv("PRISM_DATABASE_URL", url)
    monkeypatch.setenv("PRISM_KEY_PEPPER", PEPPER)
    monkeypatch.delenv("PRISM_REDIS_URL", raising=False)
    monkeypatch.delenv("PRISM_CONFIG", raising=False)
    assert runner.invoke(app, ["db-upgrade"]).exit_code == EXIT_OK
    return url


@pytest.fixture
def no_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    started: list[dict[str, object]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: started.append(kwargs))
    return started


def test_no_auth_is_refused_on_a_public_host(no_uvicorn: list[dict[str, object]]) -> None:
    result = runner.invoke(app, ["serve", "--no-auth", "--host", "0.0.0.0"])
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "loopback" in result.stderr
    assert no_uvicorn == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_no_auth_starts_on_loopback(host: str, no_uvicorn: list[dict[str, object]]) -> None:
    result = runner.invoke(app, ["serve", "--no-auth", "--host", host])
    assert result.exit_code == EXIT_OK
    assert "authentication is off" in result.stderr
    assert no_uvicorn == [{"host": host, "port": 8000}]


def test_serve_needs_a_pepper(
    monkeypatch: pytest.MonkeyPatch, no_uvicorn: list[dict[str, object]]
) -> None:
    monkeypatch.delenv("PRISM_KEY_PEPPER", raising=False)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == EXIT_MISSING_ENV
    assert "PRISM_KEY_PEPPER" in result.stderr


def test_serve_needs_a_migrated_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_uvicorn: list[dict[str, object]]
) -> None:
    monkeypatch.setenv("PRISM_KEY_PEPPER", PEPPER)
    monkeypatch.setenv("PRISM_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "prism db-upgrade" in result.stderr


def test_serve_needs_the_newest_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_uvicorn: list[dict[str, object]]
) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    upgrade(url, "0001")
    monkeypatch.setenv("PRISM_KEY_PEPPER", PEPPER)
    monkeypatch.setenv("PRISM_DATABASE_URL", url)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "at migration 0001, but this version of Prism needs 0003" in result.stderr
    assert "prism db-upgrade" in result.stderr


def test_serve_reports_a_database_it_cannot_open(
    monkeypatch: pytest.MonkeyPatch, no_uvicorn: list[dict[str, object]]
) -> None:
    monkeypatch.setenv("PRISM_KEY_PEPPER", PEPPER)
    monkeypatch.setenv("PRISM_DATABASE_URL", "sqlite+aiosqlite:////no/such/dir/x.db")
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "Cannot open the database" in result.stderr


def test_serve_starts_with_keys_enabled(
    db: str, example_config_path: Path, no_uvicorn: list[dict[str, object]]
) -> None:
    result = runner.invoke(app, ["serve", "--config", str(example_config_path)])
    assert result.exit_code == EXIT_OK
    assert len(no_uvicorn) == 1


def test_budget_commands_create_and_list_a_team_budget(db: str) -> None:
    assert runner.invoke(app, ["teams", "create", "ml"]).exit_code == EXIT_OK
    result = runner.invoke(
        app,
        ["budgets", "create", "--team", "ml", "--limit-usd", "10.50", "--period", "daily"],
    )
    assert result.exit_code == EXIT_OK
    listed = runner.invoke(app, ["budgets", "list"])
    assert listed.exit_code == EXIT_OK
    assert "daily" in listed.stdout
    assert "10.50" in listed.stdout


def test_teams_and_keys_lifecycle(db: str) -> None:
    assert "No teams yet" in runner.invoke(app, ["teams", "list"]).stdout
    assert runner.invoke(app, ["teams", "create", "ml"]).exit_code == EXIT_OK
    duplicate = runner.invoke(app, ["teams", "create", "ml"])
    assert duplicate.exit_code == EXIT_BAD_CONFIG
    assert "already exists" in duplicate.stderr
    assert "ml" in runner.invoke(app, ["teams", "list"]).stdout

    assert "No keys yet" in runner.invoke(app, ["keys", "list"]).stdout
    created = runner.invoke(
        app,
        [
            "keys", "create", "--team", "ml", "--name", "notebook",
            "--routes", "chat, embed", "--expires-in-days", "30",
        ],
    )  # fmt: skip
    assert created.exit_code == EXIT_OK
    secret = created.stdout.strip().splitlines()[-1]
    assert secret.startswith("pk-prism-")
    assert "Routes: chat, embed" in created.stdout
    assert "routes were not checked" in created.stderr  # no config to check them against
    assert "Expires:" in created.stdout

    listed = runner.invoke(app, ["keys", "list", "--team", "ml"]).stdout
    assert secret[:DISPLAY_PREFIX_LENGTH] in listed
    assert secret not in listed
    assert "chat,embed" in listed
    assert "active" in listed

    revoked = runner.invoke(app, ["keys", "revoke", secret[:DISPLAY_PREFIX_LENGTH]])
    assert revoked.exit_code == EXIT_OK
    assert "within a few seconds" in revoked.stdout
    assert "revoked" in runner.invoke(app, ["keys", "list"]).stdout


def test_teams_create_refuses_an_empty_name(db: str) -> None:
    result = runner.invoke(app, ["teams", "create", ""])
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "cannot be empty" in result.stderr
    assert "No teams yet" in runner.invoke(app, ["teams", "list"]).stdout


def test_keys_create_refuses_an_empty_name(db: str) -> None:
    assert runner.invoke(app, ["teams", "create", "ml"]).exit_code == EXIT_OK
    result = runner.invoke(app, ["keys", "create", "--team", "ml", "--name", ""])
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "cannot be empty" in result.stderr
    assert "No keys yet" in runner.invoke(app, ["keys", "list"]).stdout


def test_key_errors_are_reported_cleanly(db: str) -> None:
    unknown_team = runner.invoke(app, ["keys", "create", "--team", "ghost", "--name", "x"])
    assert unknown_team.exit_code == EXIT_BAD_CONFIG
    assert (
        "There is no team named 'ghost'. Create it with: prism teams create ghost"
        in unknown_team.stderr
    )
    bad_name = runner.invoke(app, ["keys", "create", "--team", "my team", "--name", "x"])
    assert bad_name.exit_code == EXIT_BAD_CONFIG
    assert "See the existing teams with: prism teams list" in bad_name.stderr
    assert "prism teams create" not in bad_name.stderr  # it would be refused
    unknown_key = runner.invoke(app, ["keys", "revoke", "pk-prism-none"])
    assert unknown_key.exit_code == EXIT_BAD_CONFIG


def test_keys_create_checks_routes_against_the_config(
    db: str, example_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert runner.invoke(app, ["teams", "create", "ml"]).exit_code == EXIT_OK
    base = ["keys", "create", "--team", "ml", "--name", "x", "--config", str(example_config_path)]

    known = runner.invoke(app, [*base, "--routes", "fast-chat"])
    assert known.exit_code == EXIT_OK
    assert "not checked" not in known.stderr

    typo = runner.invoke(app, [*base, "--routes", "fast-chat,fast-caht"])
    assert typo.exit_code == EXIT_BAD_CONFIG
    assert "Unknown route: fast-caht" in typo.stderr
    assert "fast-chat" in typo.stderr
    assert typo.stdout == ""  # no key was created

    monkeypatch.setenv("PRISM_CONFIG", str(example_config_path))
    from_env = runner.invoke(
        app, ["keys", "create", "--team", "ml", "--name", "y", "--routes", "nope"]
    )
    assert from_env.exit_code == EXIT_BAD_CONFIG
    assert len(runner.invoke(app, ["keys", "list"]).stdout.strip().splitlines()) == 2


def test_keys_create_needs_no_config_without_routes(db: str, tmp_path: Path) -> None:
    assert runner.invoke(app, ["teams", "create", "ml"]).exit_code == EXIT_OK
    missing = str(tmp_path / "missing.yaml")
    result = runner.invoke(
        app, ["keys", "create", "--team", "ml", "--name", "x", "--config", missing]
    )
    assert result.exit_code == EXIT_OK
    assert result.stderr == ""


def test_keys_create_refuses_a_broken_config(db: str, tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("routes: [")
    result = runner.invoke(
        app,
        ["keys", "create", "--team", "ml", "--name", "x", "--routes", "a", "--config", str(broken)],
    )
    assert result.exit_code == EXIT_BAD_CONFIG
    assert "Config is not valid" in result.stderr


def test_keys_create_needs_a_pepper(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRISM_KEY_PEPPER")
    result = runner.invoke(app, ["keys", "create", "--team", "ml", "--name", "x"])
    assert result.exit_code == EXIT_MISSING_ENV


def test_revoke_evicts_the_key_from_redis(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from fakeredis import FakeAsyncRedis, FakeServer

    from prism.core.governance.keys import hash_key
    from prism.store.redis_cache import KEY_NAMESPACE

    server = FakeServer()
    monkeypatch.setenv("PRISM_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(
        "prism.store.redis_cache.connect", lambda url: FakeAsyncRedis(server=server)
    )
    runner.invoke(app, ["teams", "create", "ml"])
    secret = runner.invoke(app, ["keys", "create", "--team", "ml", "--name", "a"]).stdout
    secret = secret.strip().splitlines()[-1]
    entry = KEY_NAMESPACE + hash_key(secret, PEPPER)

    async def seed_and_check(seed: bool) -> bool:
        client = FakeAsyncRedis(server=server)
        if seed:
            await client.set(entry, "{}")
        found = await client.exists(entry) == 1
        await client.aclose()
        return found

    import asyncio

    assert asyncio.run(seed_and_check(seed=True))
    result = runner.invoke(app, ["keys", "revoke", secret[:DISPLAY_PREFIX_LENGTH]])
    assert "stopped working now" in result.stdout
    assert not asyncio.run(seed_and_check(seed=False))


def test_revoke_still_succeeds_when_redis_is_down(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class Down:
        async def delete(self, *args: object) -> None:
            raise ConnectionError("down")

        async def aclose(self) -> None:
            return None

    monkeypatch.setenv("PRISM_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr("prism.store.redis_cache.connect", lambda url: Down())
    runner.invoke(app, ["teams", "create", "ml"])
    created = runner.invoke(app, ["keys", "create", "--team", "ml", "--name", "a"])
    secret = created.stdout.strip().splitlines()[-1]
    result = runner.invoke(app, ["keys", "revoke", secret[:DISPLAY_PREFIX_LENGTH]])
    assert result.exit_code == EXIT_OK
    assert "could not reach Redis" in result.stderr
    assert "within a few seconds" in result.stdout
