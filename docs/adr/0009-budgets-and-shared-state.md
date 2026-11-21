# 0009: Budgets, rate limits and where their state lives

Status: accepted

## Context

Budgets and rate limits need counters that change on every request. Prism runs
as one instance by default and as several instances in production.

## Decision

- **Hard budgets reject a request when `spent >= limit`.** Cost is only known
  after a call, so requests already in flight can push spend slightly past the
  limit. Reserving an estimated cost up front was rejected as too complex and as
  rejecting requests whose worst-case estimate is far above their real cost.
- **Soft budgets never block.** They log and flag usage at 80% and 100%.
- **Periods are calendar periods in UTC:** daily resets at 00:00, weekly on
  Monday, monthly on the 1st. They match invoices and the existing `reset_at`
  column. Rolling windows were rejected as expensive to compute.
- **State backend follows the deployment:**
  - One instance (no `PRISM_REDIS_URL`): counters and token buckets live in
    memory. Spend is saved to `budgets.spent_usd` every few seconds and at
    shutdown, and loaded at startup.
  - Several instances (`PRISM_REDIS_URL` set): counters and buckets live in Redis.
    Token buckets run as an atomic Lua script.
  - If Redis is unavailable, each instance falls back to its own in-memory limits
    so requests keep flowing.
- Budget and rate-limit errors return 429, with codes `budget_exceeded` and
  `rate_limited`, because OpenAI SDKs handle 429 well.

## Consequences

- Single-node installs need no extra infrastructure.
- Redis is an optional extra: `prismgw[redis]`.
- Running several instances without Redis gives each instance its own limits.
  This is documented, not detected.
