"""Adapter for the Google Gemini API (generativelanguage.googleapis.com)."""

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
    classify_status,
    extract_error_message,
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
    tool_names_by_id,
)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

_FINISH_REASONS: dict[str, FinishReason] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "IMAGE_SAFETY": "content_filter",
    "IMAGE_PROHIBITED_CONTENT": "content_filter",
    "LANGUAGE": "content_filter",
    "CONTINUATION": "length",
}

_AUTH_REASONS = {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED"}


def finish_reason_of(value: str | None, has_tool_calls: bool) -> FinishReason:
    if has_tool_calls and value in (None, "STOP"):
        return "tool_calls"
    return _FINISH_REASONS.get(value or "STOP", "stop")


class GeminiAdapter(HTTPAdapter):
    def __init__(
        self,
        name: str = "gemini",
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
                f"models/{_model_path(target.model)}:generateContent",
                self.build_payload(req),
                self.headers(key),
            )
        )
        if not data:
            raise UpstreamProtocolError("Gemini returned an unexpected response")
        return self.parse_response(data, target)

    async def chat_stream(
        self, req: ChatRequest, target: Target, key: ResolvedKey | None
    ) -> AsyncIterator[ChatChunk]:
        url = f"models/{_model_path(target.model)}:streamGenerateContent?alt=sse"
        stream_id = new_id("chatcmpl")
        usage: Usage | None = None
        finish: str | None = None
        tool_count = 0
        sent_role = False

        async with self.open_sse(url, self.build_payload(req), self.headers(key)) as events:
            async for event in events:
                data = obj(parse_json_event(event, self.name))
                if not data:
                    continue
                if "error" in data:
                    raise _stream_error(data)
                _raise_if_prompt_blocked(data)
                if data.get("usageMetadata"):
                    usage = _usage(data["usageMetadata"])
                candidates = arr(data.get("candidates"))
                if not candidates:
                    continue
                candidate = candidates[0]
                if not sent_role:
                    sent_role = True
                    yield _chunk(stream_id, target, Delta(role="assistant"))
                text, calls = _parts(candidate)
                if text:
                    yield _chunk(stream_id, target, Delta(content=text))
                if calls:
                    for call in calls:
                        call["index"] = tool_count
                        tool_count += 1
                    yield _chunk(stream_id, target, Delta(tool_calls=calls))
                if candidate.get("finishReason"):
                    finish = str(candidate["finishReason"])

        if finish is None:
            raise UpstreamProtocolError("Gemini stream ended before the answer was complete")
        yield ChatChunk(
            id=stream_id,
            model=target.model,
            choices=[ChunkChoice(finish_reason=finish_reason_of(finish, tool_count > 0))],
            usage=usage if req.wants_stream_usage else None,
        )

    def build_payload(self, req: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {"contents": convert_messages(req.messages)}
        system = system_text(req.messages)
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        config: dict[str, Any] = {}
        if req.temperature is not None:
            config["temperature"] = req.temperature
        if req.top_p is not None:
            config["topP"] = req.top_p
        tokens = req.max_completion_tokens or req.max_tokens
        if tokens is not None:
            config["maxOutputTokens"] = tokens
        stops = stop_list(req.stop)
        if stops:
            config["stopSequences"] = stops
        if req.n is not None:
            config["candidateCount"] = req.n
        if req.seed is not None:
            config["seed"] = req.seed
        config.update(_response_format(req.response_format))
        if config:
            payload["generationConfig"] = config

        tools = function_tools(req.tools)
        if tools:
            payload["tools"] = [
                {"functionDeclarations": [_function_declaration(tool) for tool in tools]}
            ]
            tool_config = _tool_config(req.tool_choice)
            if tool_config is not None:
                payload["toolConfig"] = tool_config
        return payload

    def parse_response(self, data: dict[str, Any], target: Target) -> ChatResponse:
        _raise_if_prompt_blocked(data)
        candidates = arr(data.get("candidates"))
        if not candidates:
            raise UpstreamProtocolError("Gemini returned no answer")
        choices: list[Choice] = []
        for position, candidate in enumerate(candidates):
            text, calls = _parts(candidate)
            reason = candidate.get("finishReason")
            if not text and not calls and _FINISH_REASONS.get(str(reason)) == "content_filter":
                raise ContentBlockedError(f"Gemini blocked the answer ({reason})")
            message = ChatMessage(
                role="assistant",
                content=text if text or not calls else None,
                tool_calls=calls or None,
            )
            choices.append(
                Choice(
                    index=int(candidate.get("index", position)),
                    message=message,
                    finish_reason=finish_reason_of(reason, bool(calls)),
                )
            )
        return ChatResponse(
            id=str(data.get("responseId") or new_id("chatcmpl")),
            model=str(data.get("modelVersion") or target.model),
            choices=choices,
            usage=_usage(obj(data.get("usageMetadata"))),
        )

    def headers(self, key: ResolvedKey | None) -> dict[str, str]:
        if key is None:
            return {}
        return {"x-goog-api-key": key.secret.get_secret_value()}

    def error_from_response(self, status: int, headers: Mapping[str, str], text: str) -> Exception:
        error = super().error_from_response(status, headers, text)
        if isinstance(error, UpstreamHTTPError):
            if _is_bad_key(error.body):
                error.kind = ErrorKind.AUTH_FAILED
            delay = _retry_delay(error.body)
            if delay is not None:
                error.retry_after_s = delay
        return error


def _model_path(model: str) -> str:
    return model.removeprefix("models/")


def _chunk(stream_id: str, target: Target, delta: Delta) -> ChatChunk:
    return ChatChunk(id=stream_id, model=target.model, choices=[ChunkChoice(delta=delta)])


def _usage(meta: dict[str, Any]) -> Usage:
    prompt = int(meta.get("promptTokenCount") or 0)
    completion = int(meta.get("candidatesTokenCount") or 0) + int(
        meta.get("thoughtsTokenCount") or 0
    )
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=int(meta.get("totalTokenCount") or 0) or prompt + completion,
    )


def _parts(candidate: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for raw in arr(obj(candidate.get("content")).get("parts")):
        part = obj(raw)
        if part.get("thought"):
            continue
        if "text" in part:
            texts.append(str(part["text"]))
        elif "functionCall" in part:
            function = obj(part["functionCall"])
            call: dict[str, Any] = {
                "id": function.get("id") or new_id("call"),
                "type": "function",
                "function": {
                    "name": function.get("name", ""),
                    "arguments": json.dumps(obj(function.get("args"))),
                },
            }
            if part.get("thoughtSignature"):
                call["extra_content"] = {"google": {"thought_signature": part["thoughtSignature"]}}
            calls.append(call)
    return "".join(texts), calls


def _raise_if_prompt_blocked(data: dict[str, Any]) -> None:
    feedback = obj(data.get("promptFeedback"))
    reason = feedback.get("blockReason")
    if reason and not data.get("candidates"):
        raise ContentBlockedError(f"Gemini blocked the prompt ({reason})")


def _is_bad_key(body: Any) -> bool:
    error = obj(obj(body).get("error"))
    for detail in arr(error.get("details")):
        if obj(detail).get("reason") in _AUTH_REASONS:
            return True
    return "API key not valid" in str(error.get("message", ""))


def _retry_delay(body: Any) -> float | None:
    """Gemini puts the wait in error.details[].retryDelay, such as "30s"."""
    for raw in arr(obj(obj(body).get("error")).get("details")):
        delay = str(obj(raw).get("retryDelay", ""))
        if delay.endswith("s"):
            try:
                return max(0.0, float(delay[:-1]))
            except ValueError:
                return None
    return None


def _stream_error(data: dict[str, Any]) -> UpstreamStreamError:
    error = obj(data.get("error"))
    code = error.get("code")
    kind = classify_status(code) if isinstance(code, int) else ErrorKind.UPSTREAM_UNAVAILABLE
    if kind is ErrorKind.INTERNAL_ERROR:
        kind = ErrorKind.UPSTREAM_UNAVAILABLE
    return UpstreamStreamError(extract_error_message(data, "Gemini stream error"), kind)


def _response_format(fmt: dict[str, Any] | None) -> dict[str, Any]:
    if not fmt:
        return {}
    kind = fmt.get("type")
    if kind == "json_object":
        return {"responseMimeType": "application/json"}
    if kind == "json_schema":
        schema = obj(fmt.get("json_schema")).get("schema")
        result: dict[str, Any] = {"responseMimeType": "application/json"}
        if schema:
            result["responseJsonSchema"] = schema
        return result
    return {}


def _function_declaration(tool: dict[str, Any]) -> dict[str, Any]:
    declaration: dict[str, Any] = {"name": tool["name"]}
    if tool.get("description"):
        declaration["description"] = tool["description"]
    if tool.get("parameters"):
        declaration["parametersJsonSchema"] = tool["parameters"]
    return declaration


def _tool_config(choice: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if choice is None:
        return None
    if choice == "auto":
        return {"functionCallingConfig": {"mode": "AUTO"}}
    if choice == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if choice == "required":
        return {"functionCallingConfig": {"mode": "ANY"}}
    if isinstance(choice, dict):
        name = obj(choice.get("function")).get("name")
        if name:
            return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    raise UnsupportedRequestError(f"tool_choice '{choice}' is not supported")


def _user_parts(content: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: list[dict[str, Any]] = []
    for part in content:
        kind = part.get("type")
        if kind in ("text", "input_text"):
            if part.get("text"):
                parts.append({"text": str(part["text"])})
        elif kind == "image_url":
            inline = parse_data_url(image_url_of(part))
            if inline is None:
                raise UnsupportedRequestError("Gemini images must be base64 data URLs")
            mime, data = inline
            parts.append({"inlineData": {"mimeType": mime, "data": data}})
        else:
            raise UnsupportedRequestError(f"content part type '{kind}' is not supported")
    return parts


def convert_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Turn OpenAI messages into Gemini contents (system messages are removed)."""
    names = tool_names_by_id(messages)
    contents: list[dict[str, Any]] = []

    def append(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in messages:
        if message.role in ("system", "developer"):
            continue
        if message.role == "tool":
            call_id = message.tool_call_id or ""
            raw = text_of(message.content)
            try:
                value: Any = json.loads(raw)
            except ValueError:
                value = raw
            data = obj(value)
            response = data if data or value == {} else {"result": value}
            function_response: dict[str, Any] = {
                "name": names.get(call_id, call_id),
                "response": response,
            }
            if call_id:
                function_response["id"] = call_id
            append("user", [{"functionResponse": function_response}])
        elif message.role == "assistant":
            parts = _user_parts(message.content)
            for call in message.tool_calls or []:
                function = obj(call.get("function"))
                function_call: dict[str, Any] = {
                    "name": function.get("name", ""),
                    "args": parse_tool_arguments(function.get("arguments")),
                }
                if call.get("id"):
                    function_call["id"] = call["id"]
                part: dict[str, Any] = {"functionCall": function_call}
                google = obj(obj(call.get("extra_content")).get("google"))
                signature = google.get("thought_signature")
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            append("model", parts)
        else:
            append("user", _user_parts(message.content))
    if not contents:
        raise UnsupportedRequestError("the request needs at least one non-system message")
    return contents
