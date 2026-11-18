"""The provider adapter contract.

One adapter per provider. An adapter does three jobs and nothing else:

1. send a request and return the answer in Prism's format,
2. send a request and yield the answer piece by piece,
3. say what kind of error an exception is.

Retries, fallbacks, keys and budgets are handled outside the adapter.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from prism.core.errors import ErrorKind
from prism.core.types import ChatChunk, ChatRequest, ChatResponse, ResolvedKey, Target


@runtime_checkable
class ProviderAdapter(Protocol):
    name: str

    async def chat(self, req: ChatRequest, target: Target, key: ResolvedKey | None) -> ChatResponse:
        """Send one request and return the full answer.

        Raise any exception on failure. The router calls classify_error on it.
        """
        ...

    def chat_stream(
        self, req: ChatRequest, target: Target, key: ResolvedKey | None
    ) -> AsyncIterator[ChatChunk]:
        """Send one request and yield answer pieces as they arrive.

        The last chunk carries the finish reason. If the caller asked for usage
        (req.wants_stream_usage), the last chunk also carries the token counts.
        """
        ...

    def classify_error(self, exc: Exception) -> ErrorKind:
        """Turn an exception raised by this adapter into an ErrorKind.

        Anything the adapter does not recognise must map to INTERNAL_ERROR.
        """
        ...
