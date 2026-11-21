from collections.abc import AsyncIterator

import pytest

from prism.core.context import RequestContext
from prism.core.errors import ErrorKind, PrismError
from prism.core.resilience import BreakerState
from prism.core.types import ChatChunk, ChatMessage, ChatRequest, ChatResponse, Choice, Usage
from prism.plugins.base import Continue, Decision, Reject, ShortCircuit
from prism.providers.fake import FakeProviderError
from tests.scripted import (
    RetryAfterError,
    ScriptedAdapter,
    build,
    chat_request,
    config_for,
    route,
)


def ctx_for(req: ChatRequest) -> RequestContext:
    return RequestContext(request=req, route_name=req.model)


async def drain(chunks: AsyncIterator[ChatChunk], received: list[str]) -> None:
    async for chunk in chunks:
        received.append(chunk.text)


async def collect(chunks: object) -> list[ChatChunk]:
    return [chunk async for chunk in chunks]  # type: ignore[attr-defined]


async def test_a_healthy_target_answers_first_time() -> None:
    a = ScriptedAdapter("a", reply="hello")
    h = build(config_for(["a"], [route("chat", "a")]), {"a": a})
    req = chat_request()
    result = await h.gateway.chat(req, ctx_for(req))
    assert result.response.text == "hello"
    assert result.response.model == "chat"
    assert result.target is not None
    assert (result.target.provider_id, result.attempts) == ("a", 1)
    assert h.sleeps.delays == []


async def test_a_retryable_error_is_retried_on_the_same_target() -> None:
    a = ScriptedAdapter("a", errors=[FakeProviderError(503), FakeProviderError(500)])
    h = build(config_for(["a"], [route("chat", "a")]), {"a": a})
    req = chat_request()
    result = await h.gateway.chat(req, ctx_for(req))
    assert result.attempts == 3
    assert len(h.sleeps.delays) == 2
    assert 0.1 <= h.sleeps.delays[0] <= 0.2
    assert 0.2 <= h.sleeps.delays[1] <= 0.4


async def test_after_max_retries_the_next_target_is_tried() -> None:
    a = ScriptedAdapter("a", errors=[FakeProviderError(503)] * 3)
    b = ScriptedAdapter("b", reply="from b")
    h = build(config_for(["a", "b"], [route("chat", "a", "b")]), {"a": a, "b": b})
    req = chat_request()
    result = await h.gateway.chat(req, ctx_for(req))
    assert result.response.text == "from b"
    assert len(a.calls) == 3
    assert result.attempts == 4


async def test_max_retries_zero_moves_on_at_once() -> None:
    a = ScriptedAdapter("a", errors=[FakeProviderError(503)])
    b = ScriptedAdapter("b")
    config = config_for(["a", "b"], [route("chat", "a", "b", retry={"max_retries": 0})])
    h = build(config, {"a": a, "b": b})
    req = chat_request()
    await h.gateway.chat(req, ctx_for(req))
    assert len(a.calls) == 1
    assert h.sleeps.delays == []


async def test_an_invalid_request_is_not_retried_or_failed_over() -> None:
    a = ScriptedAdapter("a", errors=[FakeProviderError(400, "bad field")])
    b = ScriptedAdapter("b")
    h = build(config_for(["a", "b"], [route("chat", "a", "b")]), {"a": a, "b": b})
    req = chat_request()
    with pytest.raises(PrismError) as caught:
        await h.gateway.chat(req, ctx_for(req))
    assert caught.value.kind is ErrorKind.INVALID_REQUEST
    assert caught.value.http_status == 400
    assert "bad field" in caught.value.client_message
    assert b.calls == []


async def test_an_unknown_route_is_a_404() -> None:
    h = build(config_for(["a"], [route("chat", "a")]), {"a": ScriptedAdapter("a")})
    req = chat_request("nope")
    with pytest.raises(PrismError) as caught:
        await h.gateway.chat(req, ctx_for(req))
    assert caught.value.kind is ErrorKind.ROUTE_NOT_FOUND
    assert caught.value.http_status == 404


async def test_an_auth_failure_disables_the_key_and_tries_the_next_target() -> None:
    a = ScriptedAdapter("a", errors=[FakeProviderError(401)])
    b = ScriptedAdapter("b")
    config = config_for(["a", "b"], [route("chat", "a", "b")], keys={"a": ["A_KEY"]})
    h = build(config, {"a": a, "b": b}, env={"A_KEY": "secret"})
    req = chat_request()
    result = await h.gateway.chat(req, ctx_for(req))
    assert result.target is not None
    assert result.target.provider_id == "b"
    assert h.pools["a"].usable_count == 0
    assert a.calls[0].key is not None
    assert a.calls[0].key.secret.get_secret_value() == "secret"


async def test_a_target_only_uses_the_keys_it_names() -> None:
    a = ScriptedAdapter("a")
    reserved = route("prod", "a")
    reserved["targets"][0]["keys"] = ["PAID"]
    config = config_for(["a"], [reserved, route("any", "a")], keys={"a": ["PAID", "FREE"]})
    h = build(config, {"a": a}, env={"PAID": "paid-secret", "FREE": "free-secret"})
    for _ in range(20):
        req = chat_request("prod")
        await h.gateway.chat(req, ctx_for(req))
    used = {call.key.secret.get_secret_value() for call in a.calls if call.key}
    assert used == {"paid-secret"}

    a.calls.clear()
    for _ in range(40):
        req = chat_request("any")
        await h.gateway.chat(req, ctx_for(req))
    used = {call.key.secret.get_secret_value() for call in a.calls if call.key}
    assert used == {"paid-secret", "free-secret"}


async def test_a_target_whose_keys_are_unusable_falls_back() -> None:
    a = ScriptedAdapter("a", errors=[FakeProviderError(401)])
    b = ScriptedAdapter("b", reply="from b")
    chat = route("chat", "a", "b")
    chat["targets"][0]["keys"] = ["A1"]
    config = config_for(["a", "b"], [chat], keys={"a": ["A1", "A2"]})
    h = build(config, {"a": a, "b": b}, env={"A1": "one", "A2": "two"})
    req = chat_request()
    first = await h.gateway.chat(req, ctx_for(req))
    assert first.response.text == "from b"  # A1 was disabled by the 401
    second = await h.gateway.chat(req, ctx_for(req))
    assert second.response.text == "from b"
    assert len(a.calls) == 1  # A2 is never used by this target, so a is skipped
    assert h.pools["a"].usable_count == 1  # A2 is still healthy for other targets


async def test_a_rate_limited_key_cools_down_and_another_key_is_used() -> None:
    a = ScriptedAdapter("a", errors=[RetryAfterError(429, 2)])
    config = config_for(["a"], [route("chat", "a")], keys={"a": ["K1", "K2"]})
    h = build(config, {"a": a}, env={"K1": "one", "K2": "two"})
    req = chat_request()
    await h.gateway.chat(req, ctx_for(req))
    first, second = (call.key for call in a.calls)
    assert first is not None
    assert second is not None
    assert first.id != second.id
    assert h.sleeps.delays == [2]


async def test_when_every_target_is_rate_limited_the_shortest_wait_is_returned() -> None:
    a = ScriptedAdapter("a", errors=[RetryAfterError(429, 30)])
    b = ScriptedAdapter("b", errors=[RetryAfterError(429, 12)])
    h = build(config_for(["a", "b"], [route("chat", "a", "b")]), {"a": a, "b": b})
    req = chat_request()
    with pytest.raises(PrismError) as caught:
        await h.gateway.chat(req, ctx_for(req))
    assert caught.value.kind is ErrorKind.RATE_LIMITED
    assert caught.value.headers() == {"Retry-After": "12"}
    assert h.sleeps.delays == []


async def test_a_long_retry_after_skips_to_the_next_target_without_waiting() -> None:
    a = ScriptedAdapter("a", errors=[RetryAfterError(429, 60)])
    b = ScriptedAdapter("b")
    h = build(config_for(["a", "b"], [route("chat", "a", "b")]), {"a": a, "b": b})
    req = chat_request()
    result = await h.gateway.chat(req, ctx_for(req))
    assert result.target is not None
    assert result.target.provider_id == "b"
    assert h.sleeps.delays == []


async def test_the_circuit_opens_after_repeated_failures_and_recovers() -> None:
    a = ScriptedAdapter("a", errors=[FakeProviderError(503)] * 3)
    b = ScriptedAdapter("b")
    config = config_for(
        ["a", "b"],
        [route("chat", "a", "b", retry={"max_retries": 0})],
        circuit_breaker={"failure_threshold": 2, "cooldown_s": 30},
    )
    h = build(config, {"a": a, "b": b})
    req = chat_request()
    for _ in range(2):
        await h.gateway.chat(req, ctx_for(req))
    assert h.gateway.breakers.get("a", "a-model").state is BreakerState.OPEN

    await h.gateway.chat(req, ctx_for(req))
    assert len(a.calls) == 2

    h.clock.advance(31)
    a.errors.clear()
    result = await h.gateway.chat(req, ctx_for(req))
    assert result.target is not None
    assert result.target.provider_id == "a"
    assert h.gateway.breakers.get("a", "a-model").state is BreakerState.CLOSED


async def test_blocked_content_fails_over_only_when_the_route_allows_it() -> None:
    blocked = FakeProviderError(400, content_blocked=True)
    a = ScriptedAdapter("a", errors=[blocked])
    b = ScriptedAdapter("b")
    strict = build(config_for(["a", "b"], [route("chat", "a", "b")]), {"a": a, "b": b})
    req = chat_request()
    with pytest.raises(PrismError) as caught:
        await strict.gateway.chat(req, ctx_for(req))
    assert caught.value.kind is ErrorKind.CONTENT_BLOCKED

    a2 = ScriptedAdapter("a", errors=[FakeProviderError(400, content_blocked=True)])
    lenient = build(
        config_for(["a", "b"], [route("chat", "a", "b", fallback_on_content_blocked=True)]),
        {"a": a2, "b": ScriptedAdapter("b")},
    )
    result = await lenient.gateway.chat(req, ctx_for(req))
    assert result.target is not None
    assert result.target.provider_id == "b"


async def test_an_adapter_bug_is_an_internal_error_without_details() -> None:
    a = ScriptedAdapter("a", errors=[RuntimeError("secret internal detail")])
    h = build(config_for(["a"], [route("chat", "a")]), {"a": a})
    req = chat_request()
    with pytest.raises(PrismError) as caught:
        await h.gateway.chat(req, ctx_for(req))
    assert caught.value.kind is ErrorKind.INTERNAL_ERROR
    assert "secret" not in caught.value.client_message


async def test_a_provider_with_no_usable_keys_is_skipped() -> None:
    config = config_for(["a", "b"], [route("chat", "a", "b")], keys={"a": ["UNSET"]})
    a = ScriptedAdapter("a")
    h = build(config, {"a": a, "b": ScriptedAdapter("b")}, env={})
    req = chat_request()
    result = await h.gateway.chat(req, ctx_for(req))
    assert result.target is not None
    assert result.target.provider_id == "b"
    assert a.calls == []


async def test_no_available_target_is_an_upstream_error() -> None:
    config = config_for(["a"], [route("chat", "a")], keys={"a": ["UNSET"]})
    h = build(config, {"a": ScriptedAdapter("a")}, env={})
    req = chat_request()
    with pytest.raises(PrismError) as caught:
        await h.gateway.chat(req, ctx_for(req))
    assert caught.value.kind is ErrorKind.UPSTREAM_UNAVAILABLE


async def test_a_stream_falls_back_before_the_first_chunk() -> None:
    a = ScriptedAdapter("a", errors=[FakeProviderError(503)] * 3)
    b = ScriptedAdapter("b", pieces=["x", "y"])
    h = build(config_for(["a", "b"], [route("chat", "a", "b")]), {"a": a, "b": b})
    req = chat_request(stream=True)
    result = await h.gateway.stream(req, ctx_for(req))
    chunks = await collect(result.chunks)
    assert "".join(c.text for c in chunks) == "xy"
    assert result.target is not None
    assert result.target.provider_id == "b"
    assert all(c.model == "chat" for c in chunks)


async def test_stream_usage_is_measured_but_only_sent_when_asked() -> None:
    a = ScriptedAdapter("a")
    h = build(config_for(["a"], [route("chat", "a")]), {"a": a})
    req = chat_request(stream=True)
    ctx = ctx_for(req)
    chunks = await collect((await h.gateway.stream(req, ctx)).chunks)
    assert all(c.usage is None for c in chunks)
    assert a.calls[0].request.wants_stream_usage
    assert ctx.metadata["usage"] == Usage(prompt_tokens=10, completion_tokens=5)

    asked = chat_request(stream=True, usage=True)
    chunks = await collect((await h.gateway.stream(asked, ctx_for(asked))).chunks)
    assert chunks[-1].usage is not None


async def test_a_failure_after_the_first_chunk_ends_the_stream_with_an_error() -> None:
    a = ScriptedAdapter("a", pieces=["one", "two", "three"], break_stream_after=1)
    b = ScriptedAdapter("b")
    h = build(config_for(["a", "b"], [route("chat", "a", "b")]), {"a": a, "b": b})
    req = chat_request(stream=True)
    result = await h.gateway.stream(req, ctx_for(req))
    received: list[str] = []
    with pytest.raises(PrismError) as caught:
        await drain(result.chunks, received)
    assert "".join(received) == "one"
    assert caught.value.kind is ErrorKind.UPSTREAM_UNAVAILABLE
    assert b.calls == []


class _Plugin:
    def __init__(self, decision: Decision) -> None:
        self.name = "test"
        self.decision = decision
        self.after: list[ChatResponse] = []

    async def before_request(self, ctx: RequestContext) -> Decision:
        return self.decision

    async def after_response(self, ctx: RequestContext, resp: ChatResponse) -> None:
        self.after.append(resp)


async def test_plugins_can_answer_reject_or_observe() -> None:
    cached = ChatResponse(
        model="chat", choices=[Choice(message=ChatMessage(role="assistant", content="cached"))]
    )
    a = ScriptedAdapter("a")
    h = build(config_for(["a"], [route("chat", "a")]), {"a": a})
    req = chat_request()

    h.gateway.plugins = (_Plugin(ShortCircuit(cached)),)
    result = await h.gateway.chat(req, ctx_for(req))
    assert (result.response.text, result.target) == ("cached", None)
    stream = await h.gateway.stream(chat_request(stream=True), ctx_for(req))
    assert "".join(c.text for c in await collect(stream.chunks)) == "cached"
    assert a.calls == []

    h.gateway.plugins = (_Plugin(Reject(PrismError(ErrorKind.INVALID_REQUEST, "nope"))),)
    with pytest.raises(PrismError):
        await h.gateway.chat(req, ctx_for(req))

    observer = _Plugin(Continue())
    h.gateway.plugins = (observer,)
    await h.gateway.chat(req, ctx_for(req))
    assert [r.text for r in observer.after] == ["ok"]


async def test_closing_the_gateway_closes_adapters() -> None:
    a = ScriptedAdapter("a")
    h = build(config_for(["a"], [route("chat", "a")]), {"a": a})
    await h.gateway.aclose()
    assert a.closed
