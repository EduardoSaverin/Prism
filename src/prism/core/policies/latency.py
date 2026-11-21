"""Latency routing: the fastest target first, by a moving average of response times."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from prism.core.context import RequestContext
from prism.core.types import Target


@dataclass
class LatencyTracker:
    """A moving average of each target's response time."""

    smoothing: float = 0.2
    _averages: dict[tuple[str, str], float] = field(default_factory=lambda: {})

    def record(self, target: Target, seconds: float) -> None:
        key = (target.provider_id, target.model)
        previous = self._averages.get(key)
        if previous is None:
            self._averages[key] = seconds
        else:
            self._averages[key] = previous + self.smoothing * (seconds - previous)

    def average(self, target: Target) -> float | None:
        return self._averages.get((target.provider_id, target.model))


@dataclass
class LatencyPolicy:
    """Fastest target first. Targets with no measurements yet go first so they get one."""

    tracker: LatencyTracker
    name: str = "latency"

    def order(self, targets: Sequence[Target], ctx: RequestContext) -> list[Target]:
        def sort_key(item: tuple[int, Target]) -> tuple[float, int]:
            position, target = item
            average = self.tracker.average(target)
            return (-1.0 if average is None else average, position)

        return [target for _, target in sorted(enumerate(targets), key=sort_key)]
