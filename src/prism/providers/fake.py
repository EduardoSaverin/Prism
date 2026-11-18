"""A pretend provider.

It answers from a script instead of calling the internet. Tests, demos and
load tests use it, and every real adapter is held to the same contract tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from prism.core.errors import ErrorKind
from prism.core.types import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Choice,
    ChunkChoice,
    Delta,
    ResolvedKey,
    Target,
    Usage,
)


class FakeProviderError(Exception):
    """The error the fake provider raises when its script says to fail."""

    def __init__(
        self,
        status: int | None,
        message: str = "fake provider error",
        *,
        timeout: bool = False,
        content_blocked: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.timeout = timeout
        self.content_blocked = content_blocked


@dataclass
class FakeScript:
    """What the fake provider should do on the next call."""

    reply: str = "Hello from the fake provider."
    prompt_tokens: int = 10
    completion_tokens: int = 5
    error: Exception | None = None
    stream_pieces: list[str] | None = None
    break_stream_after: int | None = None
    delay_s: float = 0.0
    seen_requests: list[ChatRequest] = field(default_factory=lambda: [])


class FakeAdapter:
    name = "fake"

    def __init__(self, script: FakeScript | None = None) -> None:
        self.script = script or FakeScript()

    async def chat(self, req: ChatRequest, target: Target, key: ResolvedKey | None) -> ChatResponse:
        self.script.seen_requests.append(req)
        await self._wait()
        if self.script.error is not None:
            raise self.script.error
        return ChatResponse(
            model=target.model,
            choices=[Choice(message=ChatMessage(role="assistant", content=self.script.reply))],
            usage=Usage(
                prompt_tokens=self.script.prompt_tokens,
                completion_tokens=self.script.completion_tokens,
            ),
        )

    async def chat_stream(
        self, req: ChatRequest, target: Target, key: ResolvedKey | None
    ) -> AsyncIterator[ChatChunk]:
        self.script.seen_requests.append(req)
        await self._wait()
        if self.script.error is not None:
            raise self.script.error
        pieces = self.script.stream_pieces or [self.script.reply]
        stream_id = ChatChunk(model=target.model).id
        yield ChatChunk(
            id=stream_id,
            model=target.model,
            choices=[ChunkChoice(delta=Delta(role="assistant"))],
        )
        for number, piece in enumerate(pieces):
            limit = self.script.break_stream_after
            if limit is not None and number >= limit:
                raise FakeProviderError(None, "connection dropped mid-stream")
            yield ChatChunk(
                id=stream_id,
                model=target.model,
                choices=[ChunkChoice(delta=Delta(content=piece))],
            )
        usage = None
        if req.wants_stream_usage:
            usage = Usage(
                prompt_tokens=self.script.prompt_tokens,
                completion_tokens=self.script.completion_tokens,
            )
        yield ChatChunk(
            id=stream_id,
            model=target.model,
            choices=[ChunkChoice(finish_reason="stop")],
            usage=usage,
        )

    def classify_error(self, exc: Exception) -> ErrorKind:
        if not isinstance(exc, FakeProviderError):
            return ErrorKind.INTERNAL_ERROR
        if exc.content_blocked:
            return ErrorKind.CONTENT_BLOCKED
        if exc.timeout or exc.status is None:
            return ErrorKind.UPSTREAM_UNAVAILABLE
        if exc.status == 429:
            return ErrorKind.RATE_LIMITED
        if exc.status in (401, 403):
            return ErrorKind.AUTH_FAILED
        if exc.status in (400, 404, 422):
            return ErrorKind.INVALID_REQUEST
        if exc.status >= 500:
            return ErrorKind.UPSTREAM_UNAVAILABLE
        return ErrorKind.INTERNAL_ERROR

    async def _wait(self) -> None:
        if self.script.delay_s:
            await asyncio.sleep(self.script.delay_s)
