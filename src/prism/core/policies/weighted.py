"""Weighted routing: pick targets at random, in proportion to their weight."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field

from prism.core.context import RequestContext
from prism.core.types import Target


@dataclass
class WeightedPolicy:
    """Pick the first target at random by weight, then the rest the same way."""

    rng: random.Random = field(default_factory=random.Random)
    name: str = "weighted"

    def order(self, targets: Sequence[Target], ctx: RequestContext) -> list[Target]:
        remaining = list(targets)
        ordered: list[Target] = []
        while remaining:
            chosen = self.rng.choices(remaining, weights=[t.weight for t in remaining], k=1)[0]
            ordered.append(chosen)
            remaining.remove(chosen)
        return ordered
