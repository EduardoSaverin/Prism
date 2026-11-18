# 0004: The CLI uses Typer

Status: proposed

## Context

The PRD does not name a CLI library. Phase 0 shipped the `prism` command on
`argparse` with three commands: `serve`, `check-config` and `db-upgrade`. Later
phases are expected to add admin commands, such as managing virtual keys,
budgets and routes, so the CLI will grow.

## Decision

The `prism` command is built with [Typer](https://typer.tiangolo.com/). Commands
are declared with type hints and `Annotated` options, and the console script
points at the Typer app (`prism.cli:app`).

The exit codes stay the same: 0 for success, 1 when a required key variable is
not set, and 2 for an invalid config.

## Consequences

- Command signatures are typed and checked by pyright, like the rest of the code.
- Help output, error messages and shell completion come for free.
- Adding subcommand groups (for example `prism keys create`) is straightforward.
- New runtime dependencies: `typer`, `click`, `rich` and `shellingham`.
- Tracebacks are printed without local variables (`pretty_exceptions_show_locals=False`),
  so provider keys cannot leak into terminal output.
- CLI tests use `typer.testing.CliRunner` instead of calling a `main()` function.
