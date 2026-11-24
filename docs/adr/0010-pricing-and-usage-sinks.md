# 0010: Prices come from the config, and usage goes through a sink

Status: accepted

## Context

Budgets and reports need a cost for every request, and every request should
leave a usage record. Writing to the database on the request path would add
latency, and usage data is also useful to systems outside Prism.

## Decision

- **Prices live in the YAML config,** per provider and model, in US dollars per
  million input and output tokens. A model with no price costs $0 and Prism logs
  a warning once. A database price table, editable through the admin API, may
  follow in Phase 3.
- **Usage records go through a `UsageSink` interface.** Requests put a record in
  an in-memory queue and return. A background task drains the queue in batches.
  - Default sink: batched inserts into `usage_events`.
  - Optional: a Redis Streams sink for Redis deployments, which survives an
    instance crash.
  - Optional, later: a Kafka sink as an extra (`prismgw[kafka]`) for teams that
    already run Kafka.
- If the queue is full, records are dropped with a warning. Requests are never
  blocked by usage writes.
- **Kafka is not required.** It would add a cluster to every install, and the
  batched writer already keeps the database off the request path. Budgets are
  enforced from live counters (ADR 0009), not from usage records, so a lost
  record affects reports, never enforcement.

## Implementation notes

- Prices are `Decimal`, so costs are exact. `core/pricing.py` holds `ModelPrice`,
  `PriceBook` and `build_price_book`; the gateway sets `RequestContext.usage` and
  `RequestContext.cost_usd` from the target that answered.
- The cost is not sent to clients (no response header). It stays inside Prism for
  usage records and budgets.
- `core/usage/` holds `UsageRecord`, the `UsageSink` and `UsageRecorder`
  protocols, and `BatchingUsageRecorder` (queue of 10,000, batches of 500, a
  flush every second). `store/usage.py` holds `SqlUsageSink`, which writes each
  batch with one multi-row insert. Migration `0002` adds `usage_events`.
- Records are built in the API layer when a request ends, so failures and
  streams are recorded too. Besides the PRD columns, a record keeps `team_id`
  (for team reports and budgets) and `attempts`. `key_id` and `team_id` are not
  foreign keys, so history survives deleting a key or team.
- On shutdown the recorder is asked to stop rather than cancelled, so a batch
  being written is finished, and the database closes after it.
- `prism serve` refuses to start unless the database is at the newest
  migration.
- Not done yet: partitioning `usage_events` by day on PostgreSQL. It can be
  added once the table grows large enough to need it.

## Consequences

- An instance crash can lose up to about one second of usage records with the
  default sink.
- New sinks can be added without touching the gateway.
