# 0001: Adapters receive the key the router chose

Status: proposed

## Context

The PRD sketches `chat(req, target)`. A target holds a pool of keys, and
something has to pick one. An adapter that picks its own key would need to know
about quotas, weights and key health, which are routing concerns.

## Decision

The router picks the key. Adapters get it as a third argument:
`chat(req, target, key)` and `chat_stream(req, target, key)`. The key is
`None` for providers that need none, such as a local Ollama.

## Consequences

- Adapters stay small and easy to test. They never read the environment.
- Key selection, quota tracking and disabling a bad key live in one place.
- The key travels as a `SecretStr`, so it cannot leak through logs or `repr`.
