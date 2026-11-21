# 0012: A route target can use a subset of its provider's keys

Status: accepted

## Context

Keys belong to a provider, and Prism builds one key pool per provider. Every
route target that used a provider drew from all of its keys, so a key could not
be reserved for one route. For example, a paid key could not be kept for
production, and a busy experimental route could use up the quota production
depends on. The PRD defines a target as a provider, a model and a key pool.
`Target.key_ids` already existed but was never used to pick a key.

## Decision

- A target in the config can list `keys`: env names of keys that its provider
  defines. An empty list (the default) means all of them.
- The config is refused when a target names a key its provider does not define,
  names one twice, or names any key for a provider that has none.
- There is still **one pool per provider**. `KeyPool.pick(allowed)` chooses only
  among the allowed keys. Health stays shared: a key disabled after a 401, or
  resting after a 429, is unavailable to every target. Rate limits and quotas
  belong to the key at the provider, so this is correct.
- A key id is `<provider>/<env>`, made by `prism.core.keys.key_id`, so the pool
  and the gateway agree on it.
- When none of a target's keys is usable, the target is skipped like any other
  target with no usable key, and the next one is tried.

## Consequences

- Keys can be reserved per route without duplicating providers in the config.
- A target limited to one key has no other key to fall back to on that
  provider. Its route should list a fallback target.
- A key named by a target but missing from the environment is skipped with a
  warning at startup, as before, so that target may have no usable key.
