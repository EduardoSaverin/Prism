"""Round-robin routing: each request starts at the next target in turn."""

from __future__ import annotations

import itertools
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field

from prism.core.context import RequestContext
from prism.core.types import Target


@dataclass
class RoundRobinPolicy:
    """Start each request at the next target in turn."""

    name: str = "round_robin"
    _counter: itertools.count[int] = field(default_factory=itertools.count)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def order(self, targets: Sequence[Target], ctx: RequestContext) -> list[Target]:
        if not targets:
            return []
        with self._lock:
            start = next(self._counter) % len(targets)
        items = list(targets)
        return items[start:] + items[:start]
