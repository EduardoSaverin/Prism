"""Shared HTTP plumbing for provider adapters.

Every real adapter talks to its provider over HTTP through `HTTPAdapter`. It turns
non-2xx responses, timeouts and dropped connections into a small set of exceptions,
and `classify_http_error` turns those exceptions into Prism error kinds the same
way for every provider.
"""

from __future__ import annotations

import email.utils
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import httpx

from prism.core.errors import ErrorKind

MAX_ERROR_MESSAGE_CHARS = 500


def obj(value: object) -> dict[str, Any]:
    """Treat a decoded JSON value as an object, or an empty one if it is not."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def arr(value: object) -> list[Any]:
    """Treat a decoded JSON value as a list, or an empty one if it is not."""
    return cast(list[Any], value) if isinstance(value, list) else []


class UpstreamError(Exception):
    """Base class for every failure caused by an upstream provider."""

    retry_after_s: float | None = None


class UpstreamHTTPError(UpstreamError):
    """The provider answered with a non-2xx status."""

    def __init__(
        self,
        status: int,
        message: str,
        *,
        body: Any = None,
        retry_after_s: float | None = None,
        kind: ErrorKind | None = None,
    ) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body
        self.retry_after_s = retry_after_s
        self.kind = kind


class UpstreamTimeoutError(UpstreamError):
    """The provider took too long to answer."""


class UpstreamConnectionError(UpstreamError):
    """The connection failed or dropped before the answer was complete."""


class ContentBlockedError(UpstreamError):
    """The provider refused the request or the answer on safety grounds."""


class UpstreamStreamError(UpstreamError):
    """The provider reported an error in the middle of a stream."""

    def __init__(self, message: str, kind: ErrorKind = ErrorKind.UPSTREAM_UNAVAILABLE) -> None:
        super().__init__(message)
        self.kind = kind


class UpstreamProtocolError(UpstreamError):
    """The provider sent something Prism could not understand."""


class UnsupportedRequestError(UpstreamError):
    """The request asks for something this provider cannot do."""


def classify_status(status: int) -> ErrorKind:
    if status == 429:
        return ErrorKind.RATE_LIMITED
    if status in (401, 402, 403):
        return ErrorKind.AUTH_FAILED
    if status in (400, 404, 409, 413, 415, 422):
        return ErrorKind.INVALID_REQUEST
    if status in (408, 424, 498, 499) or status >= 500:
        return ErrorKind.UPSTREAM_UNAVAILABLE
    return ErrorKind.INTERNAL_ERROR


def classify_http_error(exc: Exception) -> ErrorKind:
    """Map an exception raised by an HTTP adapter to a Prism error kind."""
    if isinstance(exc, UpstreamHTTPError):
        return exc.kind or classify_status(exc.status)
    if isinstance(exc, UpstreamStreamError):
        return exc.kind
    if isinstance(exc, ContentBlockedError):
        return ErrorKind.CONTENT_BLOCKED
    if isinstance(exc, UnsupportedRequestError):
        return ErrorKind.INVALID_REQUEST
    if isinstance(exc, UpstreamTimeoutError | UpstreamConnectionError | UpstreamProtocolError):
        return ErrorKind.UPSTREAM_UNAVAILABLE
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return ErrorKind.UPSTREAM_UNAVAILABLE
    return ErrorKind.INTERNAL_ERROR


def parse_retry_after(headers: Mapping[str, str], now: float | None = None) -> float | None:
    """Read how long the provider asked us to wait, in seconds."""
    millis = headers.get("retry-after-ms")
    if millis:
        try:
            return max(0.0, float(millis) / 1000)
        except ValueError:
            pass
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    current = time.time() if now is None else now
    return max(0.0, moment.timestamp() - current)


def _json_or_none(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def extract_error_message(body: Any, fallback: str) -> str:
    """Find the human-readable message in a provider's error body."""
    message: Any = None
    items = arr(body)
    data = obj(items[0] if items else body)
    if data:
        raw_error: Any = data.get("error", data)
        if isinstance(raw_error, str):
            message = raw_error
        else:
            error = obj(raw_error)
            message = error.get("message") or error.get("detail")
        if message is None:
            message = data.get("message") or data.get("detail")
    text = str(message) if message else fallback
    return text.strip()[:MAX_ERROR_MESSAGE_CHARS] or f"HTTP error {fallback}"


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SSEEvent]:
    """Parse a server-sent-events stream into events."""
    event = ""
    data: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                yield SSEEvent(event or "message", "\n".join(data))
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield SSEEvent(event or "message", "\n".join(data))


class HTTPAdapter:
    """Base class with the HTTP calls every real adapter needs."""

    name: str = "http"

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = httpx.Timeout(timeout_s, connect=min(10.0, timeout_s))
        self._client = client
        self._owns_client = client is None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def classify_error(self, exc: Exception) -> ErrorKind:
        return classify_http_error(exc)

    def error_from_response(self, status: int, headers: Mapping[str, str], text: str) -> Exception:
        """Build the exception for a non-2xx answer. Adapters override this for quirks."""
        body = _json_or_none(text)
        return UpstreamHTTPError(
            status,
            extract_error_message(body, text or str(status)),
            body=body,
            retry_after_s=parse_retry_after(headers),
        )

    async def post_json(
        self, path_or_url: str, payload: dict[str, Any], headers: Mapping[str, str]
    ) -> Any:
        url = self._url(path_or_url)
        try:
            response = await self.client.post(url, json=payload, headers=dict(headers))
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"timed out calling {self.name}") from exc
        except httpx.TransportError as exc:
            raise UpstreamConnectionError(f"could not reach {self.name}: {exc}") from exc
        if response.status_code >= 400:
            raise self.error_from_response(response.status_code, response.headers, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamProtocolError(f"{self.name} returned invalid JSON") from exc

    @asynccontextmanager
    async def open_sse(
        self, path_or_url: str, payload: dict[str, Any], headers: Mapping[str, str]
    ) -> AsyncGenerator[AsyncIterator[SSEEvent]]:
        """Open a streaming request and yield its parsed events."""
        url = self._url(path_or_url)
        request_headers = {"Accept": "text/event-stream", **headers}
        try:
            async with self.client.stream(
                "POST", url, json=payload, headers=request_headers
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", errors="replace")
                    raise self.error_from_response(response.status_code, response.headers, text)
                yield self._guarded(iter_sse(response.aiter_lines()))
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"timed out streaming from {self.name}") from exc
        except httpx.TransportError as exc:
            raise UpstreamConnectionError(f"stream from {self.name} failed: {exc}") from exc

    async def _guarded(self, events: AsyncIterator[SSEEvent]) -> AsyncIterator[SSEEvent]:
        try:
            async for event in events:
                yield event
        except httpx.TimeoutException as exc:
            raise UpstreamTimeoutError(f"timed out streaming from {self.name}") from exc
        except httpx.TransportError as exc:
            raise UpstreamConnectionError(f"stream from {self.name} dropped: {exc}") from exc

    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"


def parse_json_event(event: SSEEvent, provider: str) -> Any:
    try:
        return json.loads(event.data)
    except ValueError as exc:
        raise UpstreamProtocolError(f"{provider} sent an invalid stream event") from exc
