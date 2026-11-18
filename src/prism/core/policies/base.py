"""The routing policy contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from prism.core.context import RequestContext
from prism.core.types import Target


@runtime_checkable
class RoutingPolicy(Protocol):
    """Decides the order in which a route's targets are tried."""

    name: str

    def order(self, targets: Sequence[Target], ctx: RequestContext) -> list[Target]:
        """Return the targets in the order they should be tried.

        A policy may drop a target (for example, one that is out of quota),
        but it must never add one that was not passed in.
        """
        ...
