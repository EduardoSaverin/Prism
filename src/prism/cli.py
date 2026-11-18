"""The "prism" command."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from prism import __version__
from prism.config import ConfigError, PrismConfig, load_config, missing_env_vars

EXIT_OK = 0
EXIT_MISSING_ENV = 1
EXIT_BAD_CONFIG = 2

app = typer.Typer(
    name="prism",
    help="Prism AI gateway",
    no_args_is_help=True,
    add_completion=True,
    pretty_exceptions_show_locals=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"prism {__version__}")
        raise typer.Exit(EXIT_OK)


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Prism AI gateway."""


def _summary(config: PrismConfig) -> str:
    return f"{len(config.providers)} providers, {len(config.routes)} routes"


def _load_or_exit(path: Path) -> PrismConfig:
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.echo(f"Config is not valid:\n{exc}", err=True)
        raise typer.Exit(EXIT_BAD_CONFIG) from exc


@app.command("serve")
def serve(
    config: Annotated[
        Path | None, typer.Option(help="Path to the YAML config file.", dir_okay=False)
    ] = None,
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
) -> None:
    """Start the gateway."""
    import uvicorn

    from prism.main import create_app

    loaded = _load_or_exit(config) if config else None
    uvicorn.run(create_app(loaded), host=host, port=port)


@app.command("check-config")
def check_config(
    path: Annotated[Path, typer.Argument(help="Config file to check.", dir_okay=False)],
    require_env: Annotated[
        bool, typer.Option("--require-env", help="Fail if a key variable is not set.")
    ] = False,
) -> None:
    """Check a config file."""
    config = _load_or_exit(path)
    missing = missing_env_vars(config)
    typer.echo(f"Config is valid: {_summary(config)}.")
    if missing:
        typer.echo(f"Not set in the environment: {', '.join(missing)}")
        if require_env:
            raise typer.Exit(EXIT_MISSING_ENV)


@app.command("db-upgrade")
def db_upgrade(
    url: Annotated[
        str | None, typer.Option(help="Database URL (default: PRISM_DATABASE_URL).")
    ] = None,
) -> None:
    """Create or update the database tables."""
    from prism.store.db import DEFAULT_DATABASE_URL
    from prism.store.migrate import upgrade

    upgrade(url or os.environ.get("PRISM_DATABASE_URL", DEFAULT_DATABASE_URL))
    typer.echo("Database is up to date.")


if __name__ == "__main__":
    app()
