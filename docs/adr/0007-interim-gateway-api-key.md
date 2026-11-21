# 0007: An optional shared API key until virtual keys exist

Status: superseded by [0008](0008-virtual-keys.md)

## Context

Phase 1 adds `/v1/chat/completions`, which spends money on the operator's provider
keys. Per-team virtual keys, budgets and rate limits arrive in Phase 2. Without
any check, anyone who can reach the gateway could use it.

## Decision

When the `PRISM_API_KEY` environment variable is set, every `/v1` request must send
`Authorization: Bearer <PRISM_API_KEY>`. The comparison is constant-time. A missing
or wrong key returns 401 with code `invalid_api_key`. Health checks stay open.

When the variable is not set, `/v1` is open. `prism serve` listens on `127.0.0.1` by
default, so an open gateway is only reachable from the same machine unless the
operator changes `--host`.

## Consequences

- Single-user and local setups work with no extra configuration.
- Anyone exposing Prism on a network should set `PRISM_API_KEY` until Phase 2.
- Phase 2 replaces this check with virtual key lookup. The 401 error shape stays
  the same, so clients do not need to change.
