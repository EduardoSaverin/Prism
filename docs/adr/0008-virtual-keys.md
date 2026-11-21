# 0008: Virtual keys authenticate every request

Status: accepted

Supersedes: [0007](0007-interim-gateway-api-key.md)

## Context

Phase 2 adds governance: every request must be tied to a team so that budgets,
rate limits and usage can be applied to it. The shared `PRISM_API_KEY` from
ADR 0007 cannot tell clients apart and cannot be revoked for one client alone.

## Decision

- **Virtual keys replace `PRISM_API_KEY`.** The variable is no longer read.
- **Keys are always required.** A fresh install rejects every `/v1` request until
  a key exists. `prism serve --no-auth` turns authentication off for local
  development and refuses to start unless the host is a loopback address.
- **Teams and keys live in the database** and are managed with the CLI
  (`prism teams ...`, `prism keys ...`). The admin HTTP API follows in Phase 3.
- **Format:** `pk-prism-` followed by 43 URL-safe random characters (256 bits).
  The full key is shown once, at creation. The first 17 characters (`pk-prism-`
  and 8 random ones) are stored as `key_prefix` so people can tell keys apart.
  Eight random characters make two keys sharing a prefix very unlikely, so
  revoking by prefix rarely needs the key id.
- **Hashing:** HMAC-SHA256 with a server secret, `PRISM_KEY_PEPPER` (at least 32
  characters). The keys are random and long, so a slow password hash would only
  add latency. The pepper means a stolen database alone cannot be used to test
  guessed keys. Losing the pepper invalidates every key.
- **Allowed routes:** a key lists the routes it may call. An empty list allows
  every route. Any other route returns 403 `permission_denied`, and
  `GET /v1/models` lists only the allowed routes.
- **Caching and revocation:** key lookups are cached so the database is not hit on
  every request.
  - Without Redis, each instance keeps an in-memory cache.
  - With `PRISM_REDIS_URL` set, Redis is the one shared cache for all instances.
    `prism keys revoke` deletes the key from Redis, so revocation is instant
    everywhere.
  - In both modes every instance also polls the database every 2 seconds for keys
    whose `updated_at` changed, and evicts them. This bounds revocation to about
    2 seconds even when the CLI cannot reach Redis.
  - If Redis is unavailable, lookups fall back to the database.

## Consequences

- Clients keep sending `Authorization: Bearer <key>`; only the key changes.
- Running `prism serve` now needs `PRISM_KEY_PEPPER` and a migrated database
  (`prism db-upgrade`), unless `--no-auth` is used.
- `virtual_keys` gains `name`, `created_at` and `updated_at`. Migration `0001` is
  edited in place because nothing has been released yet.
