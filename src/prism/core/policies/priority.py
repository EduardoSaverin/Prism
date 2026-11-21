"""Priority routing: try targets in the order the route lists them."""

from __future__ import annotations

from collections.abc import Sequence

from prism.core.context import RequestContext
from prism.core.types import Target


class PriorityPolicy:
    """Try targets in the order the route lists them."""

    name = "priority"

    def order(self, targets: Sequence[Target], ctx: RequestContext) -> list[Target]:
        return list(targets)
