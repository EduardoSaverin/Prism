"""The "prism" command."""

from __future__ import annotations

import asyncio
import ipaddress
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import typer

from prism import __version__
from prism.config import ConfigError, PrismConfig, load_config, missing_env_vars
from prism.core.governance.budgets import BudgetMode, BudgetPeriod, BudgetScope

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


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _database_url(url: str | None) -> str:
    from prism.store.db import DEFAULT_DATABASE_URL

    return url or os.environ.get("PRISM_DATABASE_URL") or DEFAULT_DATABASE_URL


def _pepper_or_exit() -> str:
    from prism.core.governance.keys import check_pepper

    try:
        return check_pepper(os.environ.get("PRISM_KEY_PEPPER"))
    except ValueError as exc:
        typer.echo(
            f"{exc}\nSet it to a long random secret, for example the output of:\n"
            "  python -c 'import secrets; print(secrets.token_urlsafe(32))'",
            err=True,
        )
        raise typer.Exit(EXIT_MISSING_ENV) from exc


def _require_migrated(url: str) -> None:
    """Exit with a hint unless the database is at the newest migration."""
    from sqlalchemy.exc import SQLAlchemyError

    from prism.store.migrate import current_revision, head_revision

    try:
        current = current_revision(url)
    except SQLAlchemyError as exc:
        typer.echo(f"Cannot open the database: {exc}", err=True)
        raise typer.Exit(EXIT_BAD_CONFIG) from exc
    head = head_revision(url)
    if current is None:
        typer.echo("The database has no tables yet. Run: prism db-upgrade", err=True)
        raise typer.Exit(EXIT_BAD_CONFIG)
    if current != head:
        typer.echo(
            f"The database is at migration {current}, but this version of Prism needs {head}. "
            "Run: prism db-upgrade",
            err=True,
        )
        raise typer.Exit(EXIT_BAD_CONFIG)


@app.command("serve")
def serve(
    config: Annotated[
        Path | None, typer.Option(help="Path to the YAML config file.", dir_okay=False)
    ] = None,
    host: Annotated[str, typer.Option(help="Address to listen on.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on.")] = 8000,
    no_auth: Annotated[
        bool,
        typer.Option(
            "--no-auth",
            help="Accept requests without a virtual key. Only allowed on a loopback host.",
        ),
    ] = False,
) -> None:
    """Start the gateway."""
    import uvicorn

    from prism.main import build_governance, build_usage_recorder, connect_database, create_app

    if no_auth and not _is_loopback(host):
        typer.echo(
            f"--no-auth is only allowed when listening on a loopback address, not '{host}'.",
            err=True,
        )
        raise typer.Exit(EXIT_BAD_CONFIG)

    loaded = _load_or_exit(config) if config else None
    database = governance = usage = None
    if not no_auth:
        _pepper_or_exit()
        _require_migrated(_database_url(None))
        database = connect_database(os.environ)
        governance = build_governance(os.environ, database)
        usage = build_usage_recorder(database)
    try:
        web_app = create_app(
            loaded,
            governance=governance,
            usage=usage,
            database=database,
            auth_disabled=no_auth,
        )
    except ConfigError as exc:
        typer.echo(f"Config is not valid:\n{exc}", err=True)
        raise typer.Exit(EXIT_BAD_CONFIG) from exc
    if no_auth:
        typer.echo("Warning: authentication is off. Every request is accepted.", err=True)
    uvicorn.run(web_app, host=host, port=port)


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
    from prism.store.migrate import upgrade

    upgrade(_database_url(url))
    typer.echo("Database is up to date.")


# ---------- Teams and virtual keys ----------

teams_app = typer.Typer(help="Manage teams.", no_args_is_help=True)
keys_app = typer.Typer(help="Manage virtual keys.", no_args_is_help=True)
budgets_app = typer.Typer(help="Manage budgets.", no_args_is_help=True)
app.add_typer(teams_app, name="teams")
app.add_typer(keys_app, name="keys")
app.add_typer(budgets_app, name="budgets")

UrlOption = Annotated[
    str | None, typer.Option("--url", help="Database URL (default: PRISM_DATABASE_URL).")
]


def _run_admin[T](url: str | None, action: Callable[[Any], Awaitable[T]]) -> T:
    """Run one management action against the database, reporting errors cleanly."""
    from prism.store.admin import AdminError
    from prism.store.db import make_engine, make_session_factory

    async def main() -> T:
        engine = make_engine(_database_url(url))
        try:
            return await action(make_session_factory(engine))
        finally:
            await engine.dispose()

    try:
        return asyncio.run(main())
    except AdminError as exc:
        typer.echo(_with_hint(exc), err=True)
        raise typer.Exit(EXIT_BAD_CONFIG) from exc


def _with_hint(error: Exception) -> str:
    """The error message, plus the command that fixes it when there is one."""
    from prism.core.governance.teams import check_team_name
    from prism.store.team_admin import TeamNotFoundError

    if not isinstance(error, TeamNotFoundError):
        return str(error)
    try:
        check_team_name(error.name)
    except ValueError:
        return f"{error} See the existing teams with: prism teams list"
    # Valid team names only use characters that are safe to paste into a shell.
    return f"{error} Create it with: prism teams create {error.name}"


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )


@teams_app.command("create")
def teams_create(
    name: Annotated[
        str, typer.Argument(help="Unique team name: letters, digits, '.', '_' and '-'.")
    ],
    url: UrlOption = None,
) -> None:
    """Create a team."""
    from prism.store.team_admin import create_team

    team = _run_admin(url, lambda sessions: create_team(sessions, name))
    typer.echo(f"Created team '{team.name}' ({team.id}).")


@teams_app.command("list")
def teams_list(url: UrlOption = None) -> None:
    """List the teams."""
    from prism.store.team_admin import list_teams

    teams = _run_admin(url, list_teams)
    if not teams:
        typer.echo("No teams yet. Create one with: prism teams create <name>")
        return
    typer.echo(_table([["NAME", "ID"], *([t.name, t.id] for t in teams)]))


def _check_routes(routes: list[str], config: Path | None) -> None:
    """Refuse route names the config does not define. Warn when there is no config."""
    if not routes:
        return
    if config is None:
        typer.echo(
            "Warning: the routes were not checked. Pass --config or set PRISM_CONFIG.",
            err=True,
        )
        return
    known = [route.name for route in _load_or_exit(config).routes]
    unknown = [route for route in routes if route not in known]
    if unknown:
        typer.echo(
            f"Unknown route: {', '.join(unknown)}. Routes in {config}: {', '.join(known)}",
            err=True,
        )
        raise typer.Exit(EXIT_BAD_CONFIG)


@keys_app.command("create")
def keys_create(
    team: Annotated[str, typer.Option("--team", help="Team that owns the key.")],
    name: Annotated[
        str, typer.Option("--name", help="A label, such as the app using it (1-128 characters).")
    ],
    routes: Annotated[
        str | None,
        typer.Option("--routes", help="Comma-separated routes the key may use. Default: all."),
    ] = None,
    expires_in_days: Annotated[
        int | None, typer.Option("--expires-in-days", min=1, help="Expire after this many days.")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            envvar="PRISM_CONFIG",
            dir_okay=False,
            help="Config file to check --routes against (default: PRISM_CONFIG).",
        ),
    ] = None,
    url: UrlOption = None,
) -> None:
    """Create a virtual key. The key is shown once."""
    from prism.store.key_admin import create_key

    pepper = _pepper_or_exit()
    allowed = [r.strip() for r in routes.split(",") if r.strip()] if routes else []
    _check_routes(allowed, config)
    expires_at = datetime.now(UTC) + timedelta(days=expires_in_days) if expires_in_days else None
    created = _run_admin(
        url,
        lambda sessions: create_key(
            sessions,
            pepper=pepper,
            team_name=team,
            name=name,
            routes=allowed,
            expires_at=expires_at,
        ),
    )
    typer.echo(f"Created key '{name}' for team '{team}' ({created.key.id}).")
    typer.echo(f"Routes: {', '.join(allowed) if allowed else 'all'}")
    if expires_at:
        typer.echo(f"Expires: {expires_at:%Y-%m-%d %H:%M} UTC")
    typer.echo("\nCopy the key now. It cannot be shown again:\n")
    typer.echo(created.secret)


@keys_app.command("list")
def keys_list(
    team: Annotated[str | None, typer.Option("--team", help="Only this team's keys.")] = None,
    url: UrlOption = None,
) -> None:
    """List virtual keys. Secrets are never shown."""
    from prism.store.key_admin import list_keys

    listings = _run_admin(url, lambda sessions: list_keys(sessions, team))
    if not listings:
        typer.echo("No keys yet. Create one with: prism keys create --team <team> --name <name>")
        return
    rows = [["ID", "PREFIX", "NAME", "TEAM", "ROUTES", "STATUS"]]
    for item in listings:
        key = item.key
        routes_text = ",".join(key.allowed_routes) or "all"
        rows.append([key.id, key.key_prefix, key.name, item.team_name, routes_text, item.status])
    typer.echo(_table(rows))


@keys_app.command("revoke")
def keys_revoke(
    key: Annotated[str, typer.Argument(help="The key's id or its prefix (pk-prism-xxxx).")],
    url: UrlOption = None,
) -> None:
    """Revoke a virtual key. It stops working on every instance within seconds."""
    from prism.store.key_admin import revoke_key

    revoked = _run_admin(url, lambda sessions: revoke_key(sessions, key))
    redis_url = os.environ.get("PRISM_REDIS_URL")
    if redis_url:
        if asyncio.run(_evict_from_redis(redis_url, revoked.key_hash)):
            typer.echo(f"Revoked key '{revoked.name}' ({revoked.id}). It stopped working now.")
            return
        typer.echo("Warning: could not reach Redis to evict the key.", err=True)
    typer.echo(
        f"Revoked key '{revoked.name}' ({revoked.id}). "
        "Running instances stop accepting it within a few seconds."
    )


@budgets_app.command("create")
def budgets_create(
    limit: Annotated[str, typer.Option("--limit-usd", help="Spend limit in US dollars.")],
    team: Annotated[str | None, typer.Option(help="Team name.")] = None,
    key: Annotated[str | None, typer.Option(help="Virtual key id or display prefix.")] = None,
    period: Annotated[BudgetPeriod, typer.Option()] = BudgetPeriod.MONTHLY,
    mode: Annotated[BudgetMode, typer.Option()] = BudgetMode.HARD,
    url: UrlOption = None,
) -> None:
    """Create a key or team budget. Restart the gateway to load it."""
    from prism.store.budgets import create_budget

    if (team is None) == (key is None):
        raise typer.BadParameter("Specify exactly one of --team or --key.")
    try:
        limit_usd = Decimal(limit)
    except InvalidOperation as exc:
        raise typer.BadParameter("Enter a decimal amount.", param_hint="--limit-usd") from exc
    scope, target = (BudgetScope.TEAM, team) if team is not None else (BudgetScope.KEY, key)
    assert target is not None
    row = _run_admin(
        url, lambda sessions: create_budget(sessions, scope, target, limit_usd, period, mode)
    )
    typer.echo(f"Created budget {row.id}. Restart the gateway to load it.")


@budgets_app.command("list")
def budgets_list(url: UrlOption = None) -> None:
    """List budgets with their last saved spend."""
    from prism.store.budgets import SqlBudgetStore

    listings = _run_admin(url, lambda sessions: SqlBudgetStore(sessions).load())
    if not listings:
        typer.echo("No budgets yet. Create one with: prism budgets create --help")
        return
    rows = [["ID", "SCOPE", "SCOPE ID", "MODE", "PERIOD", "LIMIT USD", "SPENT USD", "RESETS UTC"]]
    for budget, spend in listings:
        rows.append(
            [
                budget.id,
                budget.scope.value,
                budget.scope_id,
                budget.mode.value,
                budget.period.value,
                format(budget.limit_usd, "f"),
                format(spend.spent_usd, "f"),
                spend.reset_at.isoformat(),
            ]
        )
    typer.echo(_table(rows))


async def _evict_from_redis(url: str, key_hash: str) -> bool:
    from prism.store.redis_cache import KEY_NAMESPACE, connect

    client = connect(url)
    try:
        await client.delete(KEY_NAMESPACE + key_hash)
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


if __name__ == "__main__":
    app()
