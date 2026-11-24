"""The per-request context handed to plugins and routing policies."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from prism.core.types import ChatRequest, Target, Usage


@dataclass
class RequestContext:
    """Everything the gateway knows about one request while it runs."""

    request: ChatRequest
    route_name: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    key_id: str | None = None
    team_id: str | None = None
    attempt: int = 0
    started_at: float = field(default_factory=time.monotonic)
    # The target being tried, then the one that answered. None if no provider was called.
    target: Target | None = None
    # True when a plugin (such as a cache) answered without calling a provider.
    cache_hit: bool = False
    # Set once the provider reports token usage. No usage means no cost.
    usage: Usage | None = None
    cost_usd: Decimal | None = None
    metadata: dict[str, Any] = field(default_factory=lambda: {})

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at
