"""The per-request context handed to plugins and routing policies."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from prism.core.types import ChatRequest


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
    metadata: dict[str, Any] = field(default_factory=lambda: {})

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at
