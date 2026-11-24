# 0011: The schema only holds what the code uses

Status: accepted

## Context

Migration `0001` created all ten tables from the PRD data model up front.
Only `teams` and `virtual_keys` were used by any code. The other eight tables,
and the `rpm` and `tpm` columns of `virtual_keys`, existed only in the schema
and in a round-trip test. They raised questions ("what is `key_pool` for?")
that the code could not answer, and their shape had never been checked
against a real use.

## Decision

- The schema holds only tables and columns that code reads or writes.
- Migration `0001` now creates `teams` and `virtual_keys` only, without `rpm`
  and `tpm`. It is edited in place because no release has shipped it.
- Each later piece of work adds its tables in its own migration, together with
  the code that uses them:

| Table or column | Added with |
| --- | --- |
| `usage_events` | Phase 2, pricing and usage sinks ([0010](0010-pricing-and-usage-sinks.md)). Added in migration `0002`. |
| `budgets` (with `mode`, see [0003](0003-schema-additions.md)) | Phase 2, budgets ([0009](0009-budgets-and-shared-state.md)) |
| `virtual_keys.rpm`, `virtual_keys.tpm` | Phase 2, rate limits ([0009](0009-budgets-and-shared-state.md)) |
| `providers`, `provider_keys`, `routes`, `route_targets`, `plugin_configs`, `audit_log` | Phase 3, admin API |

## Consequences

- The PRD data model table stays as the target design, not a description of
  the current database.
- After the first release, `0001` is frozen and every change is a new migration.
- SQLite databases created with the original `0001` can run `prism db-upgrade`.
  Migration `0002` upgrades their virtual keys and usage events in place and
  preserves the other tables. Existing usage IDs become strings. Old events
  get a team when their key still exists and record zero attempts because the
  old schema did not collect this count.
