"""Contract tests that every provider adapter must pass.

To add an adapter:

1. Write a harness. It is a small helper that makes your pretend upstream
   (for example, an httpx mock) behave the way each test asks.
2. Subclass AdapterContractSuite and give it a "harness" fixture.

The suite has no "Test" in its name on purpose, so pytest only runs the
subclasses, one per adapter.
"""

from __future__ import annotations

from typing import Protocol

import pytest
from pydantic import SecretStr

from prism.core.errors import ErrorKind
from prism.core.types import ChatChunk, ChatMessage, ChatRequest, ResolvedKey, Target
from prism.providers.base import ProviderAdapter

TARGET = Target(provider_id="test", model="test-model", key_ids=("k1",))
KEY = ResolvedKey(id="k1", secret=SecretStr("not-a-real-secret"))


class AdapterHarness(Protocol):
    """How a test controls the pretend upstream behind an adapter."""

    @property
    def adapter(self) -> ProviderAdapter: ...

    def reply_ok(self, text: str, *, prompt_tokens: int, completion_tokens: int) -> None: ...

    def reply_stream(
        self, pieces: list[str], *, prompt_tokens: int, completion_tokens: int
    ) -> None: ...

    def fail_with_status(self, status: int) -> None: ...

    def fail_timeout(self) -> None: ...

    def fail_content_blocked(self) -> None: ...

    def drop_stream_after(self, pieces: list[str], after: int) -> None: ...


def make_request(*, stream: bool = False, include_usage: bool = False) -> ChatRequest:
    return ChatRequest(
        model="test-model",
        messages=[ChatMessage(role="user", content="Say hello")],
        stream=stream,
        stream_options={"include_usage": True} if include_usage else None,
    )


async def collect(adapter: ProviderAdapter, req: ChatRequest) -> list[ChatChunk]:
    return [chunk async for chunk in adapter.chat_stream(req, TARGET, KEY)]


async def drain(adapter: ProviderAdapter, req: ChatRequest, received: list[str]) -> None:
    """Read a stream to the end, keeping the text of each piece as it arrives."""
    async for chunk in adapter.chat_stream(req, TARGET, KEY):
        received.append(chunk.text)


class AdapterContractSuite:
    @pytest.fixture
    def harness(self) -> AdapterHarness:
        raise NotImplementedError("Give your suite a 'harness' fixture.")

    def test_follows_the_adapter_protocol(self, harness: AdapterHarness) -> None:
        assert isinstance(harness.adapter, ProviderAdapter)
        assert harness.adapter.name

    async def test_chat_returns_the_answer(self, harness: AdapterHarness) -> None:
        harness.reply_ok("Hello there", prompt_tokens=7, completion_tokens=3)
        resp = await harness.adapter.chat(make_request(), TARGET, KEY)
        assert resp.text == "Hello there"
        assert resp.model

    async def test_chat_reports_token_usage(self, harness: AdapterHarness) -> None:
        harness.reply_ok("Hi", prompt_tokens=7, completion_tokens=3)
        resp = await harness.adapter.chat(make_request(), TARGET, KEY)
        assert resp.usage is not None
        assert (resp.usage.prompt_tokens, resp.usage.completion_tokens) == (7, 3)
        assert resp.usage.total_tokens == 10

    async def test_stream_pieces_add_up_to_the_answer(self, harness: AdapterHarness) -> None:
        harness.reply_stream(["Hel", "lo ", "there"], prompt_tokens=7, completion_tokens=3)
        chunks = await collect(harness.adapter, make_request(stream=True))
        assert "".join(chunk.text for chunk in chunks) == "Hello there"

    async def test_stream_has_exactly_one_finish_reason_at_the_end(
        self, harness: AdapterHarness
    ) -> None:
        harness.reply_stream(["a", "b", "c"], prompt_tokens=1, completion_tokens=1)
        chunks = await collect(harness.adapter, make_request(stream=True))
        finish = [chunk.finish_reason for chunk in chunks if chunk.finish_reason]
        assert finish == ["stop"]
        assert chunks[-1].finish_reason == "stop"

    async def test_stream_usage_appears_only_when_asked(self, harness: AdapterHarness) -> None:
        harness.reply_stream(["a", "b"], prompt_tokens=4, completion_tokens=2)
        without = await collect(harness.adapter, make_request(stream=True))
        assert all(chunk.usage is None for chunk in without)

        with_usage = await collect(harness.adapter, make_request(stream=True, include_usage=True))
        last = with_usage[-1].usage
        assert last is not None
        assert (last.prompt_tokens, last.completion_tokens) == (4, 2)

    @pytest.mark.parametrize(
        ("status", "kind"),
        [
            (429, ErrorKind.RATE_LIMITED),
            (500, ErrorKind.UPSTREAM_UNAVAILABLE),
            (503, ErrorKind.UPSTREAM_UNAVAILABLE),
            (400, ErrorKind.INVALID_REQUEST),
            (422, ErrorKind.INVALID_REQUEST),
            (401, ErrorKind.AUTH_FAILED),
            (403, ErrorKind.AUTH_FAILED),
        ],
    )
    async def test_http_errors_are_classified(
        self, harness: AdapterHarness, status: int, kind: ErrorKind
    ) -> None:
        harness.fail_with_status(status)
        with pytest.raises(Exception) as caught:  # noqa: PT011
            await harness.adapter.chat(make_request(), TARGET, KEY)
        assert harness.adapter.classify_error(caught.value) is kind

    async def test_errors_are_classified_the_same_when_streaming(
        self, harness: AdapterHarness
    ) -> None:
        harness.fail_with_status(429)
        with pytest.raises(Exception) as caught:  # noqa: PT011
            await collect(harness.adapter, make_request(stream=True))
        assert harness.adapter.classify_error(caught.value) is ErrorKind.RATE_LIMITED

    async def test_timeouts_count_as_upstream_unavailable(self, harness: AdapterHarness) -> None:
        harness.fail_timeout()
        with pytest.raises(Exception) as caught:  # noqa: PT011
            await harness.adapter.chat(make_request(), TARGET, KEY)
        assert harness.adapter.classify_error(caught.value) is ErrorKind.UPSTREAM_UNAVAILABLE

    async def test_blocked_content_is_classified(self, harness: AdapterHarness) -> None:
        harness.fail_content_blocked()
        with pytest.raises(Exception) as caught:  # noqa: PT011
            await harness.adapter.chat(make_request(), TARGET, KEY)
        assert harness.adapter.classify_error(caught.value) is ErrorKind.CONTENT_BLOCKED

    async def test_a_stream_that_drops_midway_raises_after_some_pieces(
        self, harness: AdapterHarness
    ) -> None:
        harness.drop_stream_after(["a", "b", "c", "d"], after=2)
        received: list[str] = []
        with pytest.raises(Exception) as caught:  # noqa: PT011
            await drain(harness.adapter, make_request(stream=True), received)
        assert "".join(received) == "ab"
        assert harness.adapter.classify_error(caught.value) is ErrorKind.UPSTREAM_UNAVAILABLE

    def test_unknown_errors_are_internal_errors(self, harness: AdapterHarness) -> None:
        kind = harness.adapter.classify_error(RuntimeError("a bug, not a provider problem"))
        assert kind is ErrorKind.INTERNAL_ERROR
