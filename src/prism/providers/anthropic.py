"""Adapter for the Anthropic Messages API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from prism.core.errors import ErrorKind
from prism.core.types import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Choice,
    ChunkChoice,
    Delta,
    FinishReason,
    ResolvedKey,
    Target,
    Usage,
    new_id,
)
from prism.providers.http import (
    ContentBlockedError,
    HTTPAdapter,
    UnsupportedRequestError,
    UpstreamHTTPError,
    UpstreamProtocolError,
    UpstreamStreamError,
    arr,
    obj,
    parse_json_event,
)
from prism.providers.translate import (
    function_tools,
    image_url_of,
    parse_data_url,
    parse_tool_arguments,
    stop_list,
    system_text,
    text_of,
)

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096

_STOP_REASONS: dict[str, FinishReason] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "length",
    "compaction": "length",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}

_ERROR_KINDS = {
    "overloaded_error": ErrorKind.UPSTREAM_UNAVAILABLE,
    "api_error": ErrorKind.UPSTREAM_UNAVAILABLE,
    "timeout_error": ErrorKind.UPSTREAM_UNAVAILABLE,
    "rate_limit_error": ErrorKind.RATE_LIMITED,
    "billing_error": ErrorKind.AUTH_FAILED,
    "authentication_error": ErrorKind.AUTH_FAILED,
    "permission_error": ErrorKind.AUTH_FAILED,
    "invalid_request_error": ErrorKind.INVALID_REQUEST,
    "request_too_large": ErrorKind.INVALID_REQUEST,
    "not_found_error": ErrorKind.INVALID_REQUEST,
}


def finish_reason_of(stop_reason: str | None) -> FinishReason:
    return _STOP_REASONS.get(stop_reason or "", "stop")


class AnthropicAdapter(HTTPAdapter):
    def __init__(
        self,
        name: str = "anthropic",
        *,
        base_url: str | None = None,
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        super().__init__(base_url or DEFAULT_BASE_URL, timeout_s=timeout_s, client=client)
        self.name = name

    async def chat(self, req: ChatRequest, target: Target, key: ResolvedKey | None) -> ChatResponse:
        data = obj(
            await self.post_json(
                "v1/messages", self.build_payload(req, target, stream=False), self.headers(key)
            )
        )
        if not data:
            raise UpstreamProtocolError("Anthropic returned an unexpected response")
        return self.parse_response(data, target)

    async def chat_stream(
        self, req: ChatRequest, target: Target, key: ResolvedKey | None
    ) -> AsyncIterator[ChatChunk]:
        payload = self.build_payload(req, target, stream=True)
        stream_id = new_id("chatcmpl")
        prompt_tokens = 0
        completion_tokens = 0
        stop_reason: str | None = None
        stopped = False
        tool_index: dict[int, int] = {}

        async with self.open_sse("v1/messages", payload, self.headers(key)) as events:
            async for event in events:
                data = obj(parse_json_event(event, self.name))
                if not data:
                    continue
                kind = data.get("type", event.event)
                if kind == "message_start":
                    message = obj(data.get("message"))
                    stream_id = str(message.get("id") or stream_id)
                    prompt_tokens = _input_tokens(obj(message.get("usage")))
                    completion_tokens = int(obj(message.get("usage")).get("output_tokens", 0))
                    yield _chunk(stream_id, target, Delta(role="assistant"))
                elif kind == "content_block_start":
                    block = obj(data.get("content_block"))
                    if block.get("type") == "tool_use":
                        index = len(tool_index)
                        tool_index[int(data.get("index", 0))] = index
                        call = {
                            "index": index,
                            "id": block.get("id"),
                            "type": "function",
                            "function": {"name": block.get("name"), "arguments": ""},
                        }
                        yield _chunk(stream_id, target, Delta(tool_calls=[call]))
                    elif block.get("type") == "text" and block.get("text"):
                        yield _chunk(stream_id, target, Delta(content=block["text"]))
                elif kind == "content_block_delta":
                    delta = obj(data.get("delta"))
                    if delta.get("type") == "text_delta":
                        yield _chunk(stream_id, target, Delta(content=delta.get("text", "")))
                    elif delta.get("type") == "input_json_delta":
                        index = tool_index.get(int(data.get("index", 0)))
                        if index is not None:
                            call = {
                                "index": index,
                                "function": {"arguments": delta.get("partial_json", "")},
                            }
                            yield _chunk(stream_id, target, Delta(tool_calls=[call]))
                elif kind == "message_delta":
                    stop_reason = obj(data.get("delta")).get("stop_reason") or stop_reason
                    usage = obj(data.get("usage"))
                    completion_tokens = int(usage.get("output_tokens", completion_tokens))
                    if usage.get("input_tokens") is not None:
                        prompt_tokens = _input_tokens(usage)
                elif kind == "message_stop":
                    stopped = True
                    break
                elif kind == "error":
                    raise _stream_error(data)

        if not stopped and stop_reason is None:
            raise UpstreamProtocolError("Anthropic stream ended before the message was complete")
        usage = None
        if req.wants_stream_usage:
            usage = Usage(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
        yield ChatChunk(
            id=stream_id,
            model=target.model,
            choices=[ChunkChoice(finish_reason=finish_reason_of(stop_reason))],
            usage=usage,
        )

    def build_payload(self, req: ChatRequest, target: Target, *, stream: bool) -> dict[str, Any]:
        if req.n is not None and req.n > 1:
            raise UnsupportedRequestError("Anthropic models return one choice; n must be 1")
        payload: dict[str, Any] = {
            "model": target.model,
            "messages": convert_messages(req.messages),
            "max_tokens": req.max_completion_tokens or req.max_tokens or DEFAULT_MAX_TOKENS,
            "stream": stream,
        }
        system = system_text(req.messages)
        if system:
            payload["system"] = system
        if req.temperature is not None:
            payload["temperature"] = min(req.temperature, 1.0)
        if req.top_p is not None:
            payload["top_p"] = req.top_p
        stops = stop_list(req.stop)
        if stops:
            payload["stop_sequences"] = stops
        if req.user:
            payload["metadata"] = {"user_id": req.user}
        tools = function_tools(req.tools)
        if tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("parameters") or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]
        choice = _tool_choice(req)
        if choice is not None and tools:
            payload["tool_choice"] = choice
        return payload

    def parse_response(self, data: dict[str, Any], target: Target) -> ChatResponse:
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in arr(data.get("content")):
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "tool_use":
                calls.append(
                    {
                        "id": block.get("id"),
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(obj(block.get("input"))),
                        },
                    }
                )
        stop_reason = data.get("stop_reason")
        if stop_reason == "refusal" and not texts and not calls:
            raise ContentBlockedError("Anthropic refused to answer this request")
        usage = obj(data.get("usage"))
        message = ChatMessage(
            role="assistant",
            content="".join(texts) if texts or not calls else None,
            tool_calls=calls or None,
        )
        return ChatResponse(
            id=str(data.get("id") or new_id("chatcmpl")),
            model=str(data.get("model") or target.model),
            choices=[Choice(message=message, finish_reason=finish_reason_of(stop_reason))],
            usage=Usage(
                prompt_tokens=_input_tokens(usage),
                completion_tokens=int(usage.get("output_tokens", 0)),
            ),
        )

    def headers(self, key: ResolvedKey | None) -> dict[str, str]:
        headers = {"anthropic-version": API_VERSION}
        if key is not None:
            headers["x-api-key"] = key.secret.get_secret_value()
        return headers

    def error_from_response(self, status: int, headers: Mapping[str, str], text: str) -> Exception:
        error = super().error_from_response(status, headers, text)
        if isinstance(error, UpstreamHTTPError) and status < 500:
            detail = obj(obj(error.body).get("error"))
            if detail:
                error.kind = _ERROR_KINDS.get(str(detail.get("type")))
                details = obj(detail.get("details"))
                if details.get("error_code") == "enforced_spend_limit_reached":
                    error.kind = ErrorKind.AUTH_FAILED
        return error


def _input_tokens(usage: dict[str, Any]) -> int:
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )


def _chunk(stream_id: str, target: Target, delta: Delta) -> ChatChunk:
    return ChatChunk(id=stream_id, model=target.model, choices=[ChunkChoice(delta=delta)])


def _stream_error(data: dict[str, Any]) -> UpstreamStreamError:
    detail = obj(data.get("error"))
    kind = _ERROR_KINDS.get(str(detail.get("type")), ErrorKind.UPSTREAM_UNAVAILABLE)
    return UpstreamStreamError(str(detail.get("message") or "Anthropic stream error"), kind)


def _tool_choice(req: ChatRequest) -> dict[str, Any] | None:
    choice = req.tool_choice
    parallel = req.model_extra.get("parallel_tool_calls") if req.model_extra else None
    result: dict[str, Any] | None
    if choice is None:
        result = {"type": "auto"} if parallel is False else None
    elif choice == "auto":
        result = {"type": "auto"}
    elif choice == "none":
        result = {"type": "none"}
    elif choice == "required":
        result = {"type": "any"}
    elif isinstance(choice, dict):
        name = obj(choice.get("function")).get("name")
        if not name:
            raise UnsupportedRequestError("tool_choice must name a function")
        result = {"type": "tool", "name": name}
    else:
        raise UnsupportedRequestError(f"tool_choice '{choice}' is not supported")
    if result is not None and parallel is False and result["type"] != "none":
        result["disable_parallel_tool_use"] = True
    return result


def _content_blocks(content: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content:
        kind = part.get("type")
        if kind in ("text", "input_text"):
            if part.get("text"):
                blocks.append({"type": "text", "text": str(part["text"])})
        elif kind == "image_url":
            url = image_url_of(part)
            inline = parse_data_url(url)
            if inline is not None:
                media_type, data = inline
                source = {"type": "base64", "media_type": media_type, "data": data}
            else:
                source = {"type": "url", "url": url}
            blocks.append({"type": "image", "source": source})
        else:
            raise UnsupportedRequestError(f"content part type '{kind}' is not supported")
    return blocks


def convert_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Turn OpenAI messages into Anthropic messages (system messages are removed)."""
    converted: list[dict[str, Any]] = []

    def append(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})

    for message in messages:
        if message.role in ("system", "developer"):
            continue
        if message.role == "tool":
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": text_of(message.content),
            }
            append("user", [result])
        elif message.role == "assistant":
            blocks = _content_blocks(message.content)
            for call in message.tool_calls or []:
                function = obj(call.get("function"))
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or new_id("toolu"),
                        "name": function.get("name", ""),
                        "input": parse_tool_arguments(function.get("arguments")),
                    }
                )
            append("assistant", blocks)
        else:
            append("user", _content_blocks(message.content))
    if not converted:
        raise UnsupportedRequestError("the request needs at least one non-system message")
    return converted
