"""Translation and quirks of the real provider adapters, checked on the wire."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from prism.config import ConfigError, KeyConfig, ProviderConfig, ProviderKind
from prism.core.errors import ErrorKind
from prism.core.types import ChatChunk, ChatRequest, ResolvedKey, Target
from prism.providers.anthropic import AnthropicAdapter
from prism.providers.base import ProviderAdapter
from prism.providers.gemini import GeminiAdapter
from prism.providers.http import (
    ContentBlockedError,
    SSEEvent,
    UnsupportedRequestError,
    UpstreamConnectionError,
    UpstreamHTTPError,
    UpstreamProtocolError,
    UpstreamStreamError,
    UpstreamTimeoutError,
    classify_http_error,
    classify_status,
    extract_error_message,
    iter_sse,
    parse_retry_after,
)
from prism.providers.openai_compat import OpenAICompatAdapter, normalize_finish_reason
from prism.providers.registry import PRESETS, build_adapter
from tests.contract.http_harness import MockUpstream, sse

TARGET = Target(provider_id="p", model="upstream-model", key_ids=("k1",))
KEY = ResolvedKey(id="k1", secret=SecretStr("sk-test-123"))

WEATHER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def request(**fields: Any) -> ChatRequest:
    data: dict[str, Any] = {"model": "alias", "messages": [{"role": "user", "content": "Hi"}]}
    return ChatRequest.model_validate(data | fields)


async def collect(
    adapter: ProviderAdapter, req: ChatRequest, target: Target = TARGET
) -> list[ChatChunk]:
    return [chunk async for chunk in adapter.chat_stream(req, target, KEY)]


async def drain(adapter: ProviderAdapter, req: ChatRequest, received: list[str]) -> None:
    async for chunk in adapter.chat_stream(req, TARGET, KEY):
        received.append(chunk.text)


# ---------- OpenAI-compatible ----------


def openai_compat(kind: ProviderKind) -> tuple[MockUpstream, OpenAICompatAdapter]:
    upstream = MockUpstream()
    base_url = "https://compat.test/v1" if PRESETS[kind].base_url is None else None
    adapter = OpenAICompatAdapter(
        kind.value, PRESETS[kind], base_url=base_url, client=upstream.client
    )
    return upstream, adapter


def completion(content: str = "ok", finish_reason: Any = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "upstream-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def openai_chunk(
    delta: dict[str, Any], finish_reason: str | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "upstream-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        **extra,
    }


async def test_openai_sends_bearer_auth_and_max_completion_tokens() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_json(completion())
    await adapter.chat(request(max_tokens=50, n=1, reasoning_effort="low"), TARGET, KEY)

    sent = upstream.last_request
    assert str(sent.url) == "https://api.openai.com/v1/chat/completions"
    assert sent.headers["authorization"] == "Bearer sk-test-123"
    body = upstream.last_json
    assert body["model"] == "upstream-model"
    assert body["max_completion_tokens"] == 50
    assert "max_tokens" not in body
    assert "n" not in body
    assert body["reasoning_effort"] == "low"
    assert body["stream"] is False
    assert "stream_options" not in body


async def test_openai_asks_for_stream_usage_even_when_the_caller_did_not() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_sse([sse(openai_chunk({"content": "x"}, "stop")), sse("[DONE]")])
    chunks = await collect(adapter, request(stream=True))
    assert upstream.last_json["stream_options"] == {"include_usage": True}
    assert upstream.last_request.headers["accept"] == "text/event-stream"
    assert [c.usage for c in chunks] == [None]


async def test_groq_uses_max_tokens_strips_names_and_drops_logprobs() -> None:
    upstream, adapter = openai_compat(ProviderKind.GROQ)
    upstream.respond_json(completion())
    messages = [{"role": "user", "content": "Hi", "name": "alice"}]
    await adapter.chat(
        request(messages=messages, max_completion_tokens=20, n=2, logprobs=True), TARGET, KEY
    )

    body = upstream.last_json
    assert str(upstream.last_request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert body["max_tokens"] == 20
    assert "max_completion_tokens" not in body
    assert body["n"] == 2
    assert "logprobs" not in body
    assert body["messages"] == [{"role": "user", "content": "Hi"}]


async def test_mistral_renames_seed_and_sends_no_stream_options() -> None:
    upstream, adapter = openai_compat(ProviderKind.MISTRAL)
    upstream.respond_sse(
        [
            sse(openai_chunk({"content": "x"})),
            sse(openai_chunk({}, "stop", usage={"prompt_tokens": 2, "completion_tokens": 1})),
            sse("[DONE]"),
        ]
    )
    req = request(stream=True, seed=7, user="u1", stream_options={"include_usage": True})
    chunks = await collect(adapter, req)

    body = upstream.last_json
    assert body["random_seed"] == 7
    assert "seed" not in body
    assert "user" not in body
    assert "stream_options" not in body
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 3


async def test_groq_stream_usage_is_read_from_x_groq() -> None:
    upstream, adapter = openai_compat(ProviderKind.GROQ)
    upstream.respond_sse(
        [
            sse(openai_chunk({"role": "assistant", "content": "Hi"})),
            sse(
                openai_chunk(
                    {},
                    "stop",
                    x_groq={"id": "req_1", "usage": {"prompt_tokens": 9, "completion_tokens": 2}},
                )
            ),
            sse("[DONE]"),
        ]
    )
    chunks = await collect(adapter, request(stream=True, stream_options={"include_usage": True}))
    usage = chunks[-1].usage
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (9, 2, 11)
    assert chunks[-1].finish_reason == "stop"


async def test_openai_stream_ignores_null_usage_fields_until_the_usage_chunk() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    usage_chunk = openai_chunk({}, usage={"prompt_tokens": 4, "completion_tokens": 2})
    usage_chunk["choices"] = []
    upstream.respond_sse(
        [
            sse(openai_chunk({"content": "a"}, usage=None)),
            sse(openai_chunk({}, "stop", usage=None)),
            sse(usage_chunk),
            sse("[DONE]"),
        ]
    )
    chunks = await collect(adapter, request(stream=True, stream_options={"include_usage": True}))
    assert [c.text for c in chunks] == ["a", ""]
    assert chunks[0].usage is None
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 6


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("stop", "stop"),
        ("tool_calls", "tool_calls"),
        ("eos", "stop"),
        ("model_length", "length"),
        ("end_turn", "stop"),
        ("something_new", "stop"),
        (None, None),
    ],
)
async def test_openai_finish_reasons_are_normalized(raw: Any, expected: str | None) -> None:
    assert normalize_finish_reason(raw) == expected
    upstream, adapter = openai_compat(ProviderKind.OPENAI_COMPATIBLE)
    upstream.respond_json(completion(finish_reason=raw))
    resp = await adapter.chat(request(), TARGET, None)
    assert resp.choices[0].finish_reason == expected


async def test_keyless_provider_sends_no_auth_header() -> None:
    upstream, adapter = openai_compat(ProviderKind.OLLAMA)
    upstream.respond_json(completion())
    await adapter.chat(request(tool_choice="auto", user="u"), TARGET, None)
    assert "authorization" not in upstream.last_request.headers
    assert str(upstream.last_request.url) == "http://localhost:11434/v1/chat/completions"
    assert "tool_choice" not in upstream.last_json
    assert "user" not in upstream.last_json


def test_openai_compat_needs_a_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatAdapter("cf", PRESETS[ProviderKind.CLOUDFLARE])


async def test_openai_stream_without_a_finish_reason_is_a_protocol_error() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_sse([sse(openai_chunk({"content": "a"})), sse("[DONE]")])
    with pytest.raises(UpstreamProtocolError):
        await collect(adapter, request(stream=True))


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ({"message": "overloaded"}, ErrorKind.UPSTREAM_UNAVAILABLE),
        ({"message": "slow down", "code": 429}, ErrorKind.RATE_LIMITED),
        ({"message": "teapot", "code": 418}, ErrorKind.UPSTREAM_UNAVAILABLE),
    ],
)
async def test_openai_mid_stream_error_event_raises(error: dict[str, Any], kind: ErrorKind) -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_sse([sse(openai_chunk({"content": "a"})), sse({"error": error})])
    received: list[str] = []
    with pytest.raises(UpstreamStreamError) as caught:
        await drain(adapter, request(stream=True), received)
    assert received == ["a"]
    assert str(caught.value) == error["message"]
    assert adapter.classify_error(caught.value) is kind


async def test_openai_mid_stream_content_filter_is_content_blocked() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_sse([sse({"error": {"message": "filtered", "code": "content_filter"}})])
    with pytest.raises(ContentBlockedError):
        await collect(adapter, request(stream=True))


async def test_openai_azure_style_inner_content_filter_is_content_blocked() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI_COMPATIBLE)
    body = {"error": {"message": "no", "code": "400", "innererror": {"code": "content_filtered"}}}
    upstream.respond_error(400, body)
    with pytest.raises(ContentBlockedError):
        await adapter.chat(request(), TARGET, None)


async def test_openai_rate_limit_keeps_retry_after_and_message() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_error(
        429, {"error": {"message": "Rate limit reached"}}, headers={"retry-after": "12"}
    )
    with pytest.raises(UpstreamHTTPError) as caught:
        await adapter.chat(request(), TARGET, KEY)
    assert caught.value.retry_after_s == 12
    assert caught.value.message == "Rate limit reached"
    assert adapter.classify_error(caught.value) is ErrorKind.RATE_LIMITED


async def test_openai_invalid_json_is_a_protocol_error() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_with(lambda _: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(UpstreamProtocolError):
        await adapter.chat(request(), TARGET, KEY)


@pytest.mark.parametrize(
    "body",
    [{"object": "chat.completion"}, {"choices": [{"message": {"role": "robot"}}]}],
)
async def test_openai_malformed_answer_is_a_protocol_error(body: dict[str, Any]) -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_json(body)
    with pytest.raises(UpstreamProtocolError) as caught:
        await adapter.chat(request(), TARGET, KEY)
    assert adapter.classify_error(caught.value) is ErrorKind.UPSTREAM_UNAVAILABLE


async def test_openai_malformed_stream_chunk_is_a_protocol_error() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)
    upstream.respond_sse([sse({"id": "x", "choices": [{"delta": {"role": "robot"}}]})])
    with pytest.raises(UpstreamProtocolError):
        await collect(adapter, request(stream=True))


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ({"message": "Too many requests", "code": 429}, ErrorKind.RATE_LIMITED),
        ({"message": "Bad gateway"}, ErrorKind.UPSTREAM_UNAVAILABLE),
    ],
)
async def test_openai_error_body_with_200_status_is_classified(
    error: dict[str, Any], kind: ErrorKind
) -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI_COMPATIBLE)
    upstream.respond_json({"error": error})
    with pytest.raises(UpstreamStreamError) as caught:
        await adapter.chat(request(), TARGET, None)
    assert str(caught.value) == error["message"]
    assert adapter.classify_error(caught.value) is kind


async def test_connection_failures_are_upstream_unavailable() -> None:
    upstream, adapter = openai_compat(ProviderKind.OPENAI)

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    upstream.respond_with(refuse)
    with pytest.raises(UpstreamConnectionError) as caught:
        await adapter.chat(request(), TARGET, KEY)
    assert adapter.classify_error(caught.value) is ErrorKind.UPSTREAM_UNAVAILABLE


# ---------- Anthropic ----------


def anthropic() -> tuple[MockUpstream, AnthropicAdapter]:
    upstream = MockUpstream()
    return upstream, AnthropicAdapter("anthropic", client=upstream.client)


def anthropic_message(
    content: list[dict[str, Any]], stop_reason: str = "end_turn", **usage: int
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 1, "output_tokens": 1},
    }


def event(data: dict[str, Any]) -> str:
    return sse(data, event=str(data["type"]))


async def test_anthropic_headers_url_and_defaults() -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(anthropic_message([{"type": "text", "text": "hi"}]))
    req = request(
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "developer", "content": [{"type": "text", "text": "Use metric."}]},
            {"role": "user", "content": "Hi"},
        ],
        temperature=1.7,
        top_p=0.9,
        stop="END",
        user="user-42",
    )
    await adapter.chat(req, TARGET, KEY)

    sent = upstream.last_request
    assert str(sent.url) == "https://api.anthropic.com/v1/messages"
    assert sent.headers["x-api-key"] == "sk-test-123"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in sent.headers
    body = upstream.last_json
    assert body["system"] == "Be brief.\n\nUse metric."
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}]
    assert body["max_tokens"] == 4096
    assert body["temperature"] == 1.0
    assert body["top_p"] == 0.9
    assert body["stop_sequences"] == ["END"]
    assert body["metadata"] == {"user_id": "user-42"}
    assert body["model"] == "upstream-model"
    assert body["stream"] is False


async def test_anthropic_uses_the_requested_max_tokens() -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(anthropic_message([{"type": "text", "text": "hi"}]))
    await adapter.chat(request(max_completion_tokens=300, max_tokens=100), TARGET, KEY)
    assert upstream.last_json["max_tokens"] == 300


async def test_anthropic_tools_become_input_schema() -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(anthropic_message([{"type": "text", "text": "hi"}]))
    bare = {"type": "function", "function": {"name": "now"}}
    await adapter.chat(request(tools=[WEATHER_TOOL, bare]), TARGET, KEY)
    assert upstream.last_json["tools"] == [
        {
            "name": "get_weather",
            "description": "Current weather for a city",
            "input_schema": WEATHER_TOOL["function"]["parameters"],
        },
        {"name": "now", "description": "", "input_schema": {"type": "object", "properties": {}}},
    ]
    assert "tool_choice" not in upstream.last_json


@pytest.mark.parametrize(
    ("choice", "parallel", "expected"),
    [
        ("auto", None, {"type": "auto"}),
        ("none", None, {"type": "none"}),
        ("required", None, {"type": "any"}),
        (
            {"type": "function", "function": {"name": "get_weather"}},
            None,
            {"type": "tool", "name": "get_weather"},
        ),
        ("auto", False, {"type": "auto", "disable_parallel_tool_use": True}),
        ("required", False, {"type": "any", "disable_parallel_tool_use": True}),
        (None, False, {"type": "auto", "disable_parallel_tool_use": True}),
        ("none", False, {"type": "none"}),
    ],
)
async def test_anthropic_tool_choice_mapping(
    choice: Any, parallel: bool | None, expected: dict[str, Any]
) -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(anthropic_message([{"type": "text", "text": "hi"}]))
    fields: dict[str, Any] = {"tools": [WEATHER_TOOL], "tool_choice": choice}
    if parallel is not None:
        fields["parallel_tool_calls"] = parallel
    await adapter.chat(request(**fields), TARGET, KEY)
    assert upstream.last_json["tool_choice"] == expected


def test_anthropic_rejects_unknown_tool_choice() -> None:
    _, adapter = anthropic()
    with pytest.raises(UnsupportedRequestError):
        adapter.build_payload(
            request(tools=[WEATHER_TOOL], tool_choice="sometimes"), TARGET, stream=False
        )


async def test_anthropic_tool_calls_and_results_are_translated() -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(anthropic_message([{"type": "text", "text": "Done"}]))
    calls = [
        {
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        },
        {
            "id": "toolu_2",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Oslo"}'},
        },
    ]
    messages = [
        {"role": "user", "content": "Weather in Paris and Oslo?"},
        {"role": "assistant", "content": "Checking.", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "18C"},
        {"role": "tool", "tool_call_id": "toolu_2", "content": [{"type": "text", "text": "2C"}]},
        {"role": "user", "content": "Thanks"},
    ]
    await adapter.chat(request(messages=messages, tools=[WEATHER_TOOL]), TARGET, KEY)

    assert upstream.last_json["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Weather in Paris and Oslo?"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_2",
                    "name": "get_weather",
                    "input": {"city": "Oslo"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "18C"},
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": "2C"},
                {"type": "text", "text": "Thanks"},
            ],
        },
    ]


async def test_anthropic_images_become_image_blocks() -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(anthropic_message([{"type": "text", "text": "A cat"}]))
    content = [
        {"type": "text", "text": "What is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        {"type": "image_url", "image_url": "https://example.test/cat.jpg"},
    ]
    await adapter.chat(request(messages=[{"role": "user", "content": content}]), TARGET, KEY)
    assert upstream.last_json["messages"][0]["content"] == [
        {"type": "text", "text": "What is this?"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
        },
        {"type": "image", "source": {"type": "url", "url": "https://example.test/cat.jpg"}},
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"n": 2},
        {"messages": [{"role": "system", "content": "only system"}]},
        {"messages": [{"role": "user", "content": [{"type": "input_audio"}]}]},
        {"tools": [{"type": "code_interpreter"}]},
        {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"id": "t", "function": {"name": "f", "arguments": "not json"}}],
                }
            ]
        },
    ],
)
async def test_anthropic_rejects_requests_it_cannot_express(fields: dict[str, Any]) -> None:
    upstream, adapter = anthropic()
    with pytest.raises(UnsupportedRequestError) as caught:
        await adapter.chat(request(**fields), TARGET, KEY)
    assert adapter.classify_error(caught.value) is ErrorKind.INVALID_REQUEST
    assert upstream.requests == []


async def test_anthropic_tool_use_response_becomes_tool_calls() -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(
        anthropic_message(
            [
                {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                {"type": "text", "text": "Let me look."},
                {
                    "type": "tool_use",
                    "id": "toolu_9",
                    "name": "get_weather",
                    "input": {"city": "Paris"},
                },
            ],
            "tool_use",
            input_tokens=10,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=5,
            output_tokens=20,
        )
    )
    resp = await adapter.chat(request(tools=[WEATHER_TOOL]), TARGET, KEY)

    choice = resp.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content == "Let me look."
    assert choice.message.tool_calls is not None
    [call] = choice.message.tool_calls
    assert call["id"] == "toolu_9"
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}
    assert resp.usage is not None
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens) == (115, 20)
    assert resp.usage.total_tokens == 135
    assert resp.id == "msg_1"
    assert resp.model == "claude-test"


async def test_anthropic_tool_only_response_has_no_text() -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(
        anthropic_message(
            [{"type": "tool_use", "id": "toolu_1", "name": "now", "input": {}}], "tool_use"
        )
    )
    resp = await adapter.chat(request(), TARGET, KEY)
    assert resp.choices[0].message.content is None
    assert resp.choices[0].message.tool_calls is not None


@pytest.mark.parametrize(
    ("stop_reason", "finish"),
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("stop_sequence", "stop"),
        ("refusal", "content_filter"),
        ("brand_new", "stop"),
    ],
)
async def test_anthropic_stop_reasons(stop_reason: str, finish: str) -> None:
    upstream, adapter = anthropic()
    upstream.respond_json(anthropic_message([{"type": "text", "text": "x"}], stop_reason))
    resp = await adapter.chat(request(), TARGET, KEY)
    assert resp.choices[0].finish_reason == finish


async def test_anthropic_streams_tool_calls() -> None:
    upstream, adapter = anthropic()
    upstream.respond_sse(
        [
            event(
                {
                    "type": "message_start",
                    "message": anthropic_message(
                        [], "", input_tokens=12, cache_read_input_tokens=3, output_tokens=1
                    ),
                }
            ),
            event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Checking."},
                }
            ),
            event({"type": "content_block_stop", "index": 0}),
            event(
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "get_weather",
                        "input": {},
                    },
                }
            ),
            event(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"city":'},
                }
            ),
            event(
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": ' "Paris"}'},
                }
            ),
            event({"type": "content_block_stop", "index": 1}),
            event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 30},
                }
            ),
            event({"type": "message_stop"}),
        ]
    )
    req = request(stream=True, tools=[WEATHER_TOOL], stream_options={"include_usage": True})
    chunks = await collect(adapter, req)

    assert upstream.last_json["stream"] is True
    assert upstream.last_request.headers["accept"] == "text/event-stream"
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[0].id == "msg_1"
    assert "".join(c.text for c in chunks) == "Checking."
    calls = [call for c in chunks for call in c.choices[0].delta.tool_calls or []]
    assert calls == [
        {
            "index": 0,
            "id": "toolu_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": ""},
        },
        {"index": 0, "function": {"arguments": '{"city":'}},
        {"index": 0, "function": {"arguments": ' "Paris"}'}},
    ]
    assert json.loads("".join(c["function"]["arguments"] for c in calls)) == {"city": "Paris"}
    assert [c.finish_reason for c in chunks if c.finish_reason] == ["tool_calls"]
    assert chunks[-1].usage is not None
    assert (chunks[-1].usage.prompt_tokens, chunks[-1].usage.completion_tokens) == (15, 30)


async def test_anthropic_mid_stream_overloaded_error() -> None:
    upstream, adapter = anthropic()
    upstream.respond_sse(
        [
            event({"type": "message_start", "message": anthropic_message([], "")}),
            event(
                {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
            ),
        ]
    )
    with pytest.raises(UpstreamStreamError) as caught:
        await collect(adapter, request(stream=True))
    assert str(caught.value) == "Overloaded"
    assert adapter.classify_error(caught.value) is ErrorKind.UPSTREAM_UNAVAILABLE


async def test_anthropic_stream_that_ends_early_is_a_protocol_error() -> None:
    upstream, adapter = anthropic()
    upstream.respond_sse(
        [
            event({"type": "message_start", "message": anthropic_message([], "")}),
            event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "a"},
                }
            ),
        ]
    )
    with pytest.raises(UpstreamProtocolError):
        await collect(adapter, request(stream=True))


async def test_anthropic_spend_limit_is_an_auth_failure() -> None:
    upstream, adapter = anthropic()
    upstream.respond_error(
        429,
        {
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "message": "You have reached your spend limit.",
                "details": {"error_code": "enforced_spend_limit_reached"},
            },
        },
    )
    with pytest.raises(UpstreamHTTPError) as caught:
        await adapter.chat(request(), TARGET, KEY)
    assert adapter.classify_error(caught.value) is ErrorKind.AUTH_FAILED


@pytest.mark.parametrize(
    ("status", "error_type", "kind"),
    [
        (400, "billing_error", ErrorKind.AUTH_FAILED),
        (413, "request_too_large", ErrorKind.INVALID_REQUEST),
        (429, "rate_limit_error", ErrorKind.RATE_LIMITED),
        (529, "overloaded_error", ErrorKind.UPSTREAM_UNAVAILABLE),
        (418, "brand_new_error", ErrorKind.INTERNAL_ERROR),
    ],
)
async def test_anthropic_error_types(status: int, error_type: str, kind: ErrorKind) -> None:
    upstream, adapter = anthropic()
    upstream.respond_error(
        status, {"type": "error", "error": {"type": error_type, "message": "nope"}}
    )
    with pytest.raises(UpstreamHTTPError) as caught:
        await adapter.chat(request(), TARGET, KEY)
    assert caught.value.message == "nope"
    assert adapter.classify_error(caught.value) is kind


# ---------- Gemini ----------

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash"
GEMINI_TARGET = Target(provider_id="g", model="models/gemini-2.5-flash", key_ids=("k1",))


def gemini() -> tuple[MockUpstream, GeminiAdapter]:
    upstream = MockUpstream()
    return upstream, GeminiAdapter("gemini", client=upstream.client)


def gemini_answer(
    parts: list[dict[str, Any]], finish_reason: str | None = "STOP", **extra: Any
) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts}, "index": 0}
    if finish_reason:
        candidate["finishReason"] = finish_reason
    return {
        "candidates": [candidate],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2, "totalTokenCount": 6},
        **extra,
    }


async def test_gemini_url_headers_and_contents() -> None:
    upstream, adapter = gemini()
    upstream.respond_json(gemini_answer([{"text": "Hello"}]))
    req = request(
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "How are you?"},
        ]
    )
    resp = await adapter.chat(req, GEMINI_TARGET, KEY)

    sent = upstream.last_request
    assert str(sent.url) == f"{GEMINI_URL}:generateContent"
    assert sent.headers["x-goog-api-key"] == "sk-test-123"
    assert "authorization" not in sent.headers
    body = upstream.last_json
    assert body["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "Hi"}]},
        {"role": "model", "parts": [{"text": "Hello!"}]},
        {"role": "user", "parts": [{"text": "How are you?"}]},
    ]
    assert "generationConfig" not in body
    assert resp.text == "Hello"
    assert resp.choices[0].finish_reason == "stop"


async def test_gemini_streams_from_the_sse_endpoint() -> None:
    upstream, adapter = gemini()
    upstream.respond_sse(
        [sse(gemini_answer([{"text": "Hel"}], None)), sse(gemini_answer([{"text": "lo"}]))]
    )
    chunks = await collect(adapter, request(stream=True), GEMINI_TARGET)
    sent = upstream.last_request
    assert sent.url.path == "/v1beta/models/gemini-2.5-flash:streamGenerateContent"
    assert sent.url.params["alt"] == "sse"
    assert "".join(c.text for c in chunks) == "Hello"
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage is None


async def test_gemini_generation_config_mapping() -> None:
    upstream, adapter = gemini()
    upstream.respond_json(gemini_answer([{"text": "{}"}]))
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    req = request(
        temperature=1.5,
        top_p=0.8,
        max_tokens=256,
        stop=["END", "STOP"],
        n=2,
        seed=42,
        response_format={"type": "json_schema", "json_schema": {"name": "x", "schema": schema}},
    )
    await adapter.chat(req, GEMINI_TARGET, KEY)
    assert upstream.last_json["generationConfig"] == {
        "temperature": 1.5,
        "topP": 0.8,
        "maxOutputTokens": 256,
        "stopSequences": ["END", "STOP"],
        "candidateCount": 2,
        "seed": 42,
        "responseMimeType": "application/json",
        "responseJsonSchema": schema,
    }


async def test_gemini_json_object_mode() -> None:
    upstream, adapter = gemini()
    upstream.respond_json(gemini_answer([{"text": "{}"}]))
    await adapter.chat(request(response_format={"type": "json_object"}), GEMINI_TARGET, KEY)
    assert upstream.last_json["generationConfig"] == {"responseMimeType": "application/json"}


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        (None, None),
        ("auto", {"functionCallingConfig": {"mode": "AUTO"}}),
        ("none", {"functionCallingConfig": {"mode": "NONE"}}),
        ("required", {"functionCallingConfig": {"mode": "ANY"}}),
        (
            {"type": "function", "function": {"name": "get_weather"}},
            {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["get_weather"]}},
        ),
    ],
)
async def test_gemini_tools_and_tool_choice(choice: Any, expected: Any) -> None:
    upstream, adapter = gemini()
    upstream.respond_json(gemini_answer([{"text": "ok"}]))
    bare = {"type": "function", "function": {"name": "now"}}
    await adapter.chat(request(tools=[WEATHER_TOOL, bare], tool_choice=choice), GEMINI_TARGET, KEY)
    body = upstream.last_json
    assert body["tools"] == [
        {
            "functionDeclarations": [
                {
                    "name": "get_weather",
                    "description": "Current weather for a city",
                    "parametersJsonSchema": WEATHER_TOOL["function"]["parameters"],
                },
                {"name": "now"},
            ]
        }
    ]
    assert body.get("toolConfig") == expected


async def test_gemini_tool_results_recover_the_function_name() -> None:
    upstream, adapter = gemini()
    upstream.respond_json(gemini_answer([{"text": "It is 18C."}]))
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
            "extra_content": {"google": {"thought_signature": "c2lnbmF0dXJl"}},
        },
        {"id": "call_2", "type": "function", "function": {"name": "get_time", "arguments": ""}},
    ]
    messages = [
        {"role": "user", "content": "Weather and time?"},
        {"role": "assistant", "content": None, "tool_calls": calls},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"temp": 18}'},
        {"role": "tool", "tool_call_id": "call_2", "content": "noon"},
    ]
    await adapter.chat(request(messages=messages), GEMINI_TARGET, KEY)
    assert upstream.last_json["contents"] == [
        {"role": "user", "parts": [{"text": "Weather and time?"}]},
        {
            "role": "model",
            "parts": [
                {
                    "functionCall": {
                        "name": "get_weather",
                        "args": {"city": "Paris"},
                        "id": "call_1",
                    },
                    "thoughtSignature": "c2lnbmF0dXJl",
                },
                {"functionCall": {"name": "get_time", "args": {}, "id": "call_2"}},
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "functionResponse": {
                        "name": "get_weather",
                        "response": {"temp": 18},
                        "id": "call_1",
                    }
                },
                {
                    "functionResponse": {
                        "name": "get_time",
                        "response": {"result": "noon"},
                        "id": "call_2",
                    }
                },
            ],
        },
    ]


async def test_gemini_images_must_be_inline() -> None:
    upstream, adapter = gemini()
    upstream.respond_json(gemini_answer([{"text": "A cat"}]))
    image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ"}}
    await adapter.chat(request(messages=[{"role": "user", "content": [image]}]), GEMINI_TARGET, KEY)
    assert upstream.last_json["contents"][0]["parts"] == [
        {"inlineData": {"mimeType": "image/jpeg", "data": "/9j/4AAQ"}}
    ]

    remote = {"type": "image_url", "image_url": {"url": "https://example.test/cat.jpg"}}
    with pytest.raises(UnsupportedRequestError):
        await adapter.chat(
            request(messages=[{"role": "user", "content": [remote]}]), GEMINI_TARGET, KEY
        )


async def test_gemini_function_call_response_becomes_tool_calls() -> None:
    upstream, adapter = gemini()
    upstream.respond_json(
        gemini_answer(
            [
                {"text": "Planning the call...", "thought": True},
                {
                    "functionCall": {"name": "get_weather", "args": {"city": "Paris"}},
                    "thoughtSignature": "c2ln",
                },
            ],
            modelVersion="gemini-2.5-flash-001",
            responseId="resp-1",
        )
    )
    resp = await adapter.chat(request(tools=[WEATHER_TOOL]), GEMINI_TARGET, KEY)

    choice = resp.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content is None
    assert choice.message.tool_calls is not None
    [call] = choice.message.tool_calls
    assert call["type"] == "function"
    assert call["id"]
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}
    assert call["extra_content"] == {"google": {"thought_signature": "c2ln"}}
    assert resp.model == "gemini-2.5-flash-001"
    assert resp.id == "resp-1"


async def test_gemini_thought_signature_round_trips() -> None:
    upstream, adapter = gemini()
    upstream.respond_json(
        gemini_answer([{"functionCall": {"name": "now", "args": {}}, "thoughtSignature": "abc"}])
    )
    first = await adapter.chat(request(), GEMINI_TARGET, KEY)
    assistant = first.choices[0].message.model_dump(exclude_none=True)
    [call] = assistant["tool_calls"]

    upstream.respond_json(gemini_answer([{"text": "It is noon."}]))
    messages = [
        {"role": "user", "content": "Time?"},
        assistant,
        {"role": "tool", "tool_call_id": call["id"], "content": "12:00"},
    ]
    await adapter.chat(request(messages=messages), GEMINI_TARGET, KEY)
    model_turn = upstream.last_json["contents"][1]
    assert model_turn["parts"][0]["thoughtSignature"] == "abc"
    tool_turn = upstream.last_json["contents"][2]
    assert tool_turn["parts"][0]["functionResponse"]["name"] == "now"


async def test_gemini_thought_parts_are_skipped() -> None:
    upstream, adapter = gemini()
    upstream.respond_json(
        gemini_answer([{"text": "secret plan", "thought": True}, {"text": "Answer"}])
    )
    resp = await adapter.chat(request(), GEMINI_TARGET, KEY)
    assert resp.text == "Answer"


async def test_gemini_usage_counts_thought_tokens() -> None:
    upstream, adapter = gemini()
    body = gemini_answer([{"text": "x"}])
    body["usageMetadata"] = {
        "promptTokenCount": 5,
        "candidatesTokenCount": 2,
        "thoughtsTokenCount": 10,
        "totalTokenCount": 17,
    }
    upstream.respond_json(body)
    resp = await adapter.chat(request(), GEMINI_TARGET, KEY)
    assert resp.usage is not None
    assert (resp.usage.prompt_tokens, resp.usage.completion_tokens) == (5, 12)
    assert resp.usage.total_tokens == 17


async def test_gemini_streams_function_calls() -> None:
    upstream, adapter = gemini()
    upstream.respond_sse(
        [
            sse(gemini_answer([{"functionCall": {"name": "a", "args": {}}}], None)),
            sse(gemini_answer([{"functionCall": {"name": "b", "args": {"x": 1}}}])),
        ]
    )
    chunks = await collect(
        adapter, request(stream=True, stream_options={"include_usage": True}), GEMINI_TARGET
    )
    calls = [call for c in chunks for call in c.choices[0].delta.tool_calls or []]
    assert [(c["index"], c["function"]["name"]) for c in calls] == [(0, "a"), (1, "b")]
    assert chunks[-1].finish_reason == "tool_calls"
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.total_tokens == 6


async def test_gemini_mid_stream_error() -> None:
    upstream, adapter = gemini()
    upstream.respond_sse(
        [
            sse(gemini_answer([{"text": "a"}], None)),
            sse({"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}}),
        ]
    )
    with pytest.raises(UpstreamStreamError) as caught:
        await collect(adapter, request(stream=True), GEMINI_TARGET)
    assert adapter.classify_error(caught.value) is ErrorKind.UPSTREAM_UNAVAILABLE


async def test_gemini_stream_without_finish_reason_is_a_protocol_error() -> None:
    upstream, adapter = gemini()
    upstream.respond_sse([sse(gemini_answer([{"text": "a"}], None))])
    with pytest.raises(UpstreamProtocolError):
        await collect(adapter, request(stream=True), GEMINI_TARGET)


async def test_gemini_bad_key_is_an_auth_failure() -> None:
    upstream, adapter = gemini()
    upstream.respond_error(
        400,
        {
            "error": {
                "code": 400,
                "message": "Request rejected.",
                "status": "INVALID_ARGUMENT",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID",
                        "domain": "googleapis.com",
                    }
                ],
            }
        },
    )
    with pytest.raises(UpstreamHTTPError) as caught:
        await adapter.chat(request(), GEMINI_TARGET, KEY)
    assert adapter.classify_error(caught.value) is ErrorKind.AUTH_FAILED


async def test_gemini_retry_delay_is_read_from_retry_info() -> None:
    upstream, adapter = gemini()
    upstream.respond_error(
        429,
        {
            "error": {
                "code": 429,
                "message": "Quota exceeded.",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []},
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "30s"},
                ],
            }
        },
    )
    with pytest.raises(UpstreamHTTPError) as caught:
        await adapter.chat(request(), GEMINI_TARGET, KEY)
    assert caught.value.retry_after_s == 30
    assert caught.value.message == "Quota exceeded."
    assert adapter.classify_error(caught.value) is ErrorKind.RATE_LIMITED


async def test_gemini_safety_stop_without_content_is_blocked() -> None:
    upstream, adapter = gemini()
    upstream.respond_json({"candidates": [{"finishReason": "SAFETY", "index": 0}]})
    with pytest.raises(ContentBlockedError) as caught:
        await adapter.chat(request(), GEMINI_TARGET, KEY)
    assert adapter.classify_error(caught.value) is ErrorKind.CONTENT_BLOCKED


async def test_gemini_answer_without_candidates_is_a_protocol_error() -> None:
    upstream, adapter = gemini()
    upstream.respond_json({"usageMetadata": {"promptTokenCount": 1}})
    with pytest.raises(UpstreamProtocolError):
        await adapter.chat(request(), GEMINI_TARGET, KEY)


# ---------- shared HTTP behaviour ----------


@pytest.mark.parametrize("make", [anthropic, gemini])
async def test_stream_timeouts_are_upstream_unavailable(
    make: Callable[[], tuple[MockUpstream, AnthropicAdapter | GeminiAdapter]],
) -> None:
    upstream, adapter = make()
    upstream.respond_timeout()
    with pytest.raises(UpstreamTimeoutError) as caught:
        await collect(adapter, request(stream=True))
    assert adapter.classify_error(caught.value) is ErrorKind.UPSTREAM_UNAVAILABLE


async def test_stream_timeout_on_openai_compat() -> None:
    upstream, adapter = openai_compat(ProviderKind.GROQ)
    upstream.respond_timeout()
    with pytest.raises(UpstreamTimeoutError):
        await collect(adapter, request(stream=True))


async def test_stream_error_status_keeps_retry_after() -> None:
    upstream, adapter = anthropic()
    upstream.respond_error(
        429,
        {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}},
        headers={"retry-after": "4"},
    )
    with pytest.raises(UpstreamHTTPError) as caught:
        await collect(adapter, request(stream=True))
    assert caught.value.retry_after_s == 4
    assert adapter.classify_error(caught.value) is ErrorKind.RATE_LIMITED


async def test_non_json_error_body_keeps_the_text() -> None:
    upstream, adapter = gemini()
    upstream.respond_error(502, "<html>Bad Gateway</html>")
    with pytest.raises(UpstreamHTTPError) as caught:
        await adapter.chat(request(), GEMINI_TARGET, KEY)
    assert caught.value.message == "<html>Bad Gateway</html>"
    assert caught.value.body is None
    assert adapter.classify_error(caught.value) is ErrorKind.UPSTREAM_UNAVAILABLE


# ---------- http.py helpers ----------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "7"}, 7.0),
        ({"retry-after": "1.5"}, 1.5),
        ({"retry-after": "-3"}, 0.0),
        ({"retry-after-ms": "250"}, 0.25),
        ({"retry-after-ms": "250", "retry-after": "9"}, 0.25),
        ({"retry-after-ms": "soon", "retry-after": "9"}, 9.0),
        ({"retry-after": "Wed, 21 Oct 2015 07:28:30 GMT"}, 30.0),
        ({"retry-after": "Wed, 21 Oct 2015 07:27:00 GMT"}, 0.0),
        ({"retry-after": "whenever"}, None),
        ({"retry-after": ""}, None),
        ({}, None),
    ],
)
def test_parse_retry_after(headers: dict[str, str], expected: float | None) -> None:
    now = 1445412480.0  # Wed, 21 Oct 2015 07:28:00 GMT
    assert parse_retry_after(headers, now=now) == expected


async def lines_of(text: str) -> AsyncIterator[str]:
    for line in text.split("\n"):
        yield line


async def parse_sse(text: str) -> list[SSEEvent]:
    return [e async for e in iter_sse(lines_of(text))]


async def test_iter_sse_handles_events_comments_and_multiline_data() -> None:
    text = (
        ": keep-alive\n"
        "\n"
        "event: message_start\n"
        'data: {"a": 1}\n'
        "\n"
        "data: first\r\n"
        "data:second\n"
        "id: 7\n"
        "\n"
        "event: lonely\n"
        "\n"
        "data: tail"
    )
    assert await parse_sse(text) == [
        SSEEvent("message_start", '{"a": 1}'),
        SSEEvent("message", "first\nsecond"),
        SSEEvent("message", "tail"),
    ]


async def test_iter_sse_resets_the_event_name_between_events() -> None:
    events = await parse_sse("event: ping\ndata: 1\n\ndata: 2\n\n")
    assert [e.event for e in events] == ["ping", "message"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": {"message": "openai style"}}, "openai style"),
        (
            {"type": "error", "error": {"type": "x", "message": "anthropic style"}},
            "anthropic style",
        ),
        ([{"error": {"code": 400, "message": "gemini list"}}], "gemini list"),
        ({"error": "plain string"}, "plain string"),
        ({"error": {"detail": "detail inside"}}, "detail inside"),
        ({"message": "top level"}, "top level"),
        ({"detail": "fastapi style"}, "fastapi style"),
        ({"other": 1}, "fallback text"),
        (None, "fallback text"),
        ("just a string", "fallback text"),
        ({"error": {"message": "  padded  "}}, "padded"),
    ],
)
def test_extract_error_message(body: Any, expected: str) -> None:
    assert extract_error_message(body, "fallback text") == expected


def test_extract_error_message_truncates_long_messages() -> None:
    assert len(extract_error_message({"message": "x" * 2000}, "f")) == 500


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (429, ErrorKind.RATE_LIMITED),
        (401, ErrorKind.AUTH_FAILED),
        (402, ErrorKind.AUTH_FAILED),
        (403, ErrorKind.AUTH_FAILED),
        (400, ErrorKind.INVALID_REQUEST),
        (404, ErrorKind.INVALID_REQUEST),
        (409, ErrorKind.INVALID_REQUEST),
        (413, ErrorKind.INVALID_REQUEST),
        (415, ErrorKind.INVALID_REQUEST),
        (422, ErrorKind.INVALID_REQUEST),
        (408, ErrorKind.UPSTREAM_UNAVAILABLE),
        (424, ErrorKind.UPSTREAM_UNAVAILABLE),
        (498, ErrorKind.UPSTREAM_UNAVAILABLE),
        (499, ErrorKind.UPSTREAM_UNAVAILABLE),
        (500, ErrorKind.UPSTREAM_UNAVAILABLE),
        (502, ErrorKind.UPSTREAM_UNAVAILABLE),
        (504, ErrorKind.UPSTREAM_UNAVAILABLE),
        (529, ErrorKind.UPSTREAM_UNAVAILABLE),
        (418, ErrorKind.INTERNAL_ERROR),
        (302, ErrorKind.INTERNAL_ERROR),
    ],
)
def test_classify_status(status: int, kind: ErrorKind) -> None:
    assert classify_status(status) is kind


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (UpstreamHTTPError(500, "x"), ErrorKind.UPSTREAM_UNAVAILABLE),
        (UpstreamHTTPError(500, "x", kind=ErrorKind.AUTH_FAILED), ErrorKind.AUTH_FAILED),
        (UpstreamStreamError("x", ErrorKind.RATE_LIMITED), ErrorKind.RATE_LIMITED),
        (ContentBlockedError("x"), ErrorKind.CONTENT_BLOCKED),
        (UnsupportedRequestError("x"), ErrorKind.INVALID_REQUEST),
        (UpstreamTimeoutError("x"), ErrorKind.UPSTREAM_UNAVAILABLE),
        (UpstreamConnectionError("x"), ErrorKind.UPSTREAM_UNAVAILABLE),
        (UpstreamProtocolError("x"), ErrorKind.UPSTREAM_UNAVAILABLE),
        (ValueError("x"), ErrorKind.INTERNAL_ERROR),
    ],
)
def test_classify_http_error(exc: Exception, kind: ErrorKind) -> None:
    assert classify_http_error(exc) is kind


# ---------- registry ----------


def provider(kind: ProviderKind, base_url: str | None = None) -> ProviderConfig:
    return ProviderConfig(id="p1", kind=kind, base_url=base_url, keys=(KeyConfig(env="TEST_KEY"),))


@pytest.mark.parametrize(
    ("kind", "adapter_type"),
    [
        (ProviderKind.ANTHROPIC, AnthropicAdapter),
        (ProviderKind.GEMINI, GeminiAdapter),
        (ProviderKind.OPENAI, OpenAICompatAdapter),
        (ProviderKind.GROQ, OpenAICompatAdapter),
        (ProviderKind.MISTRAL, OpenAICompatAdapter),
        (ProviderKind.CEREBRAS, OpenAICompatAdapter),
        (ProviderKind.NVIDIA_NIM, OpenAICompatAdapter),
        (ProviderKind.OLLAMA, OpenAICompatAdapter),
    ],
)
def test_build_adapter_picks_the_adapter_for_each_kind(
    kind: ProviderKind, adapter_type: type
) -> None:
    adapter = build_adapter(provider(kind))
    assert isinstance(adapter, adapter_type)
    assert adapter.name == "p1"
    if isinstance(adapter, OpenAICompatAdapter):
        assert adapter.preset is PRESETS[kind]


@pytest.mark.parametrize("kind", [ProviderKind.CLOUDFLARE, ProviderKind.OPENAI_COMPATIBLE])
def test_build_adapter_uses_the_configured_base_url(kind: ProviderKind) -> None:
    adapter = build_adapter(provider(kind, "https://llm.test/v1/"))
    assert isinstance(adapter, OpenAICompatAdapter)
    assert adapter.base_url == "https://llm.test/v1"


def test_every_openai_compatible_kind_has_a_preset() -> None:
    native = {ProviderKind.ANTHROPIC, ProviderKind.GEMINI, ProviderKind.GITHUB_MODELS}
    assert set(PRESETS) == set(ProviderKind) - native


def test_github_models_is_retired() -> None:
    with pytest.raises(ConfigError, match="retired"):
        build_adapter(provider(ProviderKind.GITHUB_MODELS))
