"""Adapter for every provider that speaks the OpenAI chat completions API.

OpenAI, Groq, Mistral, Cerebras, NVIDIA NIM, GitHub Models, Cloudflare Workers AI,
Ollama and self-hosted servers such as vLLM all accept the same request shape.
Their differences are small, so each one is described by an `OpenAICompatPreset`
instead of a separate class.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from prism.core.errors import ErrorKind
from prism.core.types import ChatChunk, ChatRequest, ChatResponse, ResolvedKey, Target, Usage
from prism.providers.http import (
    ContentBlockedError,
    HTTPAdapter,
    UpstreamProtocolError,
    UpstreamStreamError,
    arr,
    classify_status,
    extract_error_message,
    obj,
    parse_json_event,
)

TokenField = Literal["max_tokens", "max_completion_tokens"]

_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "function_call"}
_FINISH_ALIASES = {"model_length": "length", "eos": "stop", "end_turn": "stop"}
_CONTENT_FILTER_CODES = {"content_filter", "content_policy_violation", "content_filtered"}


@dataclass(frozen=True)
class OpenAICompatPreset:
    """What makes one OpenAI-compatible provider different from the others."""

    base_url: str | None
    requires_key: bool = True
    stream_usage_option: bool = True
    token_field: TokenField = "max_tokens"
    drop_params: frozenset[str] = frozenset()
    strip_message_names: bool = False
    rename_params: Mapping[str, str] = field(default_factory=lambda: {})
    extra_headers: Mapping[str, str] = field(default_factory=lambda: {})


def normalize_finish_reason(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = _FINISH_ALIASES.get(text, text)
    return text if text in _FINISH_REASONS else "stop"


def _normalize_choices(data: dict[str, Any]) -> None:
    for choice in arr(data.get("choices")):
        if isinstance(choice, dict) and "finish_reason" in choice:
            choice["finish_reason"] = normalize_finish_reason(choice["finish_reason"])


class OpenAICompatAdapter(HTTPAdapter):
    """Talks to any OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        name: str,
        preset: OpenAICompatPreset,
        *,
        base_url: str | None = None,
        timeout_s: float = 60.0,
        client: Any = None,
    ) -> None:
        url = base_url or preset.base_url
        if not url:
            raise ValueError(f"provider '{name}' needs a base_url")
        super().__init__(url, timeout_s=timeout_s, client=client)
        self.name = name
        self.preset = preset

    async def chat(self, req: ChatRequest, target: Target, key: ResolvedKey | None) -> ChatResponse:
        payload = self.build_payload(req, target, stream=False)
        data = obj(await self.post_json("chat/completions", payload, self.headers(key)))
        if not data:
            raise UpstreamProtocolError(f"{self.name} returned an unexpected response")
        if "error" in data and not data.get("choices"):
            raise self._stream_error(data)
        data.setdefault("model", target.model)
        data["object"] = "chat.completion"
        _normalize_choices(data)
        try:
            return ChatResponse.model_validate(data)
        except ValidationError as exc:
            raise UpstreamProtocolError(f"{self.name} returned an unexpected response") from exc

    async def chat_stream(
        self, req: ChatRequest, target: Target, key: ResolvedKey | None
    ) -> AsyncIterator[ChatChunk]:
        payload = self.build_payload(req, target, stream=True)
        finished: list[ChatChunk] = []
        usage: Usage | None = None
        async with self.open_sse("chat/completions", payload, self.headers(key)) as events:
            async for event in events:
                if event.data.strip() == "[DONE]":
                    break
                data = obj(parse_json_event(event, self.name))
                if not data:
                    continue
                if "error" in data and not data.get("choices"):
                    raise self._stream_error(data)
                data.setdefault("model", target.model)
                data["object"] = "chat.completion.chunk"
                _normalize_choices(data)
                groq_usage = obj(data.get("x_groq")).get("usage")
                if not data.get("usage") and isinstance(groq_usage, dict):
                    data["usage"] = groq_usage
                try:
                    chunk = ChatChunk.model_validate(data)
                except ValidationError as exc:
                    raise UpstreamProtocolError(
                        f"{self.name} sent an unexpected stream event"
                    ) from exc
                if chunk.usage is not None:
                    usage = chunk.usage
                chunk.usage = None
                if not chunk.choices:
                    continue
                if any(choice.finish_reason for choice in chunk.choices):
                    finished.append(chunk)
                    continue
                yield chunk
        if not finished:
            raise UpstreamProtocolError(f"stream from {self.name} ended without a finish reason")
        if req.wants_stream_usage:
            finished[-1].usage = usage
        for chunk in finished:
            yield chunk

    def build_payload(self, req: ChatRequest, target: Target, *, stream: bool) -> dict[str, Any]:
        payload = req.model_dump(exclude_none=True)
        payload["model"] = target.model
        payload["stream"] = stream
        payload.pop("stream_options", None)
        if stream and self.preset.stream_usage_option:
            payload["stream_options"] = {"include_usage": True}

        tokens = payload.pop("max_completion_tokens", None) or payload.pop("max_tokens", None)
        payload.pop("max_tokens", None)
        if tokens is not None:
            payload[self.preset.token_field] = tokens

        if payload.get("n") == 1:
            payload.pop("n")
        if self.preset.strip_message_names:
            for message in payload["messages"]:
                message.pop("name", None)
        for name in self.preset.drop_params:
            payload.pop(name, None)
        for old, new in self.preset.rename_params.items():
            if old in payload:
                payload[new] = payload.pop(old)
        return payload

    def headers(self, key: ResolvedKey | None) -> dict[str, str]:
        headers = dict(self.preset.extra_headers)
        if key is not None:
            headers["Authorization"] = f"Bearer {key.secret.get_secret_value()}"
        return headers

    def error_from_response(self, status: int, headers: Mapping[str, str], text: str) -> Exception:
        error = super().error_from_response(status, headers, text)
        body = getattr(error, "body", None)
        if _is_content_filter(body):
            return ContentBlockedError(getattr(error, "message", "content blocked"))
        return error

    def _stream_error(self, data: dict[str, Any]) -> Exception:
        message = extract_error_message(data, "stream error")
        if _is_content_filter(data):
            return ContentBlockedError(message)
        status = obj(data.get("error")).get("code")
        kind = (
            classify_status(status) if isinstance(status, int) else ErrorKind.UPSTREAM_UNAVAILABLE
        )
        if kind is ErrorKind.INTERNAL_ERROR:
            kind = ErrorKind.UPSTREAM_UNAVAILABLE
        return UpstreamStreamError(message, kind)


def _is_content_filter(body: Any) -> bool:
    data = obj(body)
    error = obj(data.get("error", data))
    if not error:
        return False
    values = {str(error.get("code", "")), str(error.get("type", ""))}
    values.add(str(obj(error.get("innererror")).get("code", "")))
    return bool(values & _CONTENT_FILTER_CODES)
