"""The plugin contract.

A plugin can look at a request before it goes to a provider, and at the answer
after it comes back. Guardrails, PII redaction and the cache are all plugins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from prism.core.context import RequestContext
from prism.core.errors import PrismError
from prism.core.types import ChatResponse


@dataclass(frozen=True)
class Continue:
    """Let the request carry on to the next plugin or the provider."""


@dataclass(frozen=True)
class ShortCircuit:
    """Answer right now and skip the provider. A cache hit does this."""

    response: ChatResponse


@dataclass(frozen=True)
class Reject:
    """Stop the request with an error."""

    error: PrismError


Decision = Continue | ShortCircuit | Reject


@runtime_checkable
class Plugin(Protocol):
    name: str

    async def before_request(self, ctx: RequestContext) -> Decision:
        """Look at ctx.request and return Continue, ShortCircuit or Reject."""
        ...

    async def after_response(self, ctx: RequestContext, resp: ChatResponse) -> None:
        """Run once the answer is ready. Used for logging and cache writes."""
        ...
