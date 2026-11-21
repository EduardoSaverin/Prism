# 0003: Three small additions to the data model

Status: proposed

## Context

Other parts of the PRD need columns that its data model table does not list.

## Decision

| Addition | Why |
| --- | --- |
| `virtual_keys.key_prefix` | Section 9: keys keep "a short prefix for identification" |
| `virtual_keys.revoked_at` | Section 9: a stolen key needs "instant revocation" |
| `budgets.mode` (`hard` or `soft`) | Section 4: "Limits are soft or hard" |

## Consequences

- `virtual_keys.key_prefix` and `virtual_keys.revoked_at` are in migration `0001`.
  `budgets.mode` arrives with the budgets table ([0011](0011-schema-grows-with-the-code.md)).
- A test fails if the models and the migration ever disagree.
- The PRD data model table should be updated to list them.
