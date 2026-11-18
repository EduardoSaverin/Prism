from pathlib import Path

import pytest
from typer.testing import CliRunner

from prism import __version__
from prism.cli import EXIT_BAD_CONFIG, EXIT_MISSING_ENV, EXIT_OK, app

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
