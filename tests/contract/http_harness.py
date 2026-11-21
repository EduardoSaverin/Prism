"""A pretend HTTP upstream for testing the real provider adapters.

`MockUpstream` sits behind an `httpx.MockTransport`. It records every request the
adapter sends and answers with whatever the test asked for: a JSON body, an SSE
stream, an error, a timeout or a stream that drops halfway.

`HTTPHarness` builds the contract-suite harness on top of it. A subclass says how
its provider spells a success, a stream, an error and a blocked answer, and the
base class turns that into the methods `AdapterHarness` needs.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx

from prism.providers.base import ProviderAdapter

Responder = Callable[[httpx.Request], httpx.Response]


def sse(data: Any, event: str | None = None) -> str:
    """One server-sent event, JSON-encoded unless it is already a string."""
    payload = data if isinstance(data, str) else json.dumps(data)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n"


async def _byte_stream(events: list[str], *, then_drop: bool) -> AsyncIterator[bytes]:
    for event in events:
        yield event.encode()
    if then_drop:
        raise httpx.ReadError("connection reset by peer")


class MockUpstream:
    """Records requests and answers them the way the current test asked."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._responder: Responder = self._unset
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responder(request)

    @staticmethod
    def _unset(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    @property
    def last_request(self) -> httpx.Request:
        assert self.requests, "the adapter sent no request"
        return self.requests[-1]

    @property
    def last_json(self) -> dict[str, Any]:
        return json.loads(self.last_request.content)

    def respond_with(self, responder: Responder) -> None:
        self._responder = responder

    def respond_json(
        self, body: Any, *, status: int = 200, headers: Mapping[str, str] | None = None
    ) -> None:
        self._responder = lambda _: httpx.Response(status, json=body, headers=headers)

    def respond_sse(self, events: list[str]) -> None:
        self.respond_sse_with(lambda _: events)

    def respond_sse_with(self, events_for: Callable[[httpx.Request], list[str]]) -> None:
        """Stream events built from the request, so the stream can depend on what was asked."""
        self._responder = lambda request: _sse_response(events_for(request), then_drop=False)

    def respond_dropped_stream(self, events: list[str]) -> None:
        self._responder = lambda _: _sse_response(events, then_drop=True)

    def respond_error(
        self, status: int, body: Any, *, headers: Mapping[str, str] | None = None
    ) -> None:
        if isinstance(body, str):
            self._responder = lambda _: httpx.Response(status, text=body, headers=headers)
        else:
            self.respond_json(body, status=status, headers=headers)

    def respond_timeout(self) -> None:
        def raise_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        self._responder = raise_timeout


def _sse_response(events: list[str], *, then_drop: bool) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_byte_stream(events, then_drop=then_drop),
    )


class HTTPHarness(MockUpstream):
    """Contract-suite harness for an adapter that talks HTTP.

    Subclasses set `self.adapter` (built with `client=self.client`) and describe
    their provider's wire format by overriding the methods below.
    """

    adapter: ProviderAdapter

    def success_body(self, text: str, prompt_tokens: int, completion_tokens: int) -> Any:
        raise NotImplementedError

    def stream_start(self) -> list[str]:
        return []

    def stream_piece(self, piece: str) -> list[str]:
        raise NotImplementedError

    def stream_end(
        self, request: httpx.Request, prompt_tokens: int, completion_tokens: int
    ) -> list[str]:
        raise NotImplementedError

    def error_body(self, status: int) -> Any:
        raise NotImplementedError

    def blocked_response(self) -> tuple[int, Any]:
        raise NotImplementedError

    def reply_ok(self, text: str, *, prompt_tokens: int, completion_tokens: int) -> None:
        self.respond_json(self.success_body(text, prompt_tokens, completion_tokens))

    def reply_stream(
        self, pieces: list[str], *, prompt_tokens: int, completion_tokens: int
    ) -> None:
        def events_for(request: httpx.Request) -> list[str]:
            events = self.stream_start()
            for piece in pieces:
                events += self.stream_piece(piece)
            return events + self.stream_end(request, prompt_tokens, completion_tokens)

        self.respond_sse_with(events_for)

    def fail_with_status(self, status: int) -> None:
        self.respond_error(status, self.error_body(status))

    def fail_timeout(self) -> None:
        self.respond_timeout()

    def fail_content_blocked(self) -> None:
        status, body = self.blocked_response()
        self.respond_json(body, status=status)

    def drop_stream_after(self, pieces: list[str], after: int) -> None:
        events = self.stream_start()
        for piece in pieces[:after]:
            events += self.stream_piece(piece)
        self.respond_dropped_stream(events)
