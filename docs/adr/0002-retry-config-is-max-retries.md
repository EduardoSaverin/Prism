# 0002: The retry setting is `max_retries`

Status: proposed

## Context

The PRD config example uses `max_attempts: 2`, but the text says a target is
retried "at most twice". Those are different numbers: two retries means three
attempts.

## Decision

The setting is `max_retries`, the number of extra tries after the first one.
The default is 2, and the allowed range is 0 to 5.

## Consequences

- The name says what the number means, so nobody has to guess.
- The PRD example (section 5, "Config schema") should change to match.
