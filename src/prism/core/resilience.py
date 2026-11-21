"""Retry delays and circuit breakers."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

Clock = Callable[[], float]

MAX_BACKOFF_S = 2.0


def backoff_delay(
    attempt: int,
    base_s: float,
    *,
    rng: random.Random | None = None,
    cap_s: float = MAX_BACKOFF_S,
) -> float:
    """Delay before retry number `attempt` (0 for the first retry).

    Exponential growth capped at `cap_s`, with 50 to 100 percent jitter so that
    many clients do not retry in lockstep.
    """
    if base_s <= 0:
        return 0.0
    ceiling = min(cap_s, base_s * (2**attempt))
    jitter = (rng or random).uniform(0.5, 1.0)
    return ceiling * jitter


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Stops sending traffic to a failing target until it recovers.

    Closed: traffic flows. After `failure_threshold` failures in a row it opens.
    Open: traffic is refused until `cooldown_s` has passed.
    Half-open: one probe request is let through. Success closes the breaker,
    failure opens it again for another cooldown.
    """

    failure_threshold: int = 5
    cooldown_s: float = 30.0
    clock: Clock = time.monotonic
    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    opened_at: float = 0.0
    probe_in_flight: bool = False

    def allow(self) -> bool:
        """True when a request may be sent now. May move open to half-open."""
        if self.state is BreakerState.CLOSED:
            return True
        if self.state is BreakerState.OPEN:
            if self.clock() - self.opened_at < self.cooldown_s:
                return False
            self.state = BreakerState.HALF_OPEN
            self.probe_in_flight = False
        if self.probe_in_flight:
            return False
        self.probe_in_flight = True
        return True

    def record_success(self) -> None:
        self.state = BreakerState.CLOSED
        self.failures = 0
        self.probe_in_flight = False

    def record_failure(self) -> None:
        self.probe_in_flight = False
        if self.state is BreakerState.HALF_OPEN:
            self._open()
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self._open()

    def release(self) -> None:
        """Forget a probe that ended without a verdict, such as a client error."""
        self.probe_in_flight = False

    def _open(self) -> None:
        self.state = BreakerState.OPEN
        self.opened_at = self.clock()
        self.failures = 0


@dataclass
class BreakerRegistry:
    """One circuit breaker per (provider, model) target."""

    failure_threshold: int = 5
    cooldown_s: float = 30.0
    clock: Clock = time.monotonic
    _breakers: dict[tuple[str, str], CircuitBreaker] = field(default_factory=lambda: {})

    def get(self, provider_id: str, model: str) -> CircuitBreaker:
        key = (provider_id, model)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(self.failure_threshold, self.cooldown_s, self.clock)
            self._breakers[key] = breaker
        return breaker

    def states(self) -> dict[tuple[str, str], BreakerState]:
        return {key: breaker.state for key, breaker in self._breakers.items()}
