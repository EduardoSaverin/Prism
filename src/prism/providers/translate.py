"""Helpers shared by adapters that translate the OpenAI format to another one."""

from __future__ import annotations

import json
import re
from typing import Any

from prism.core.types import ChatMessage
from prism.providers.http import UnsupportedRequestError, obj

_DATA_URL = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+);base64,(?P<data>.+)$", re.DOTALL)


def text_of(content: str | list[dict[str, Any]] | None) -> str:
    """The plain text of a message's content."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(
        str(part.get("text", "")) for part in content if part.get("type") in ("text", "input_text")
    )


def parse_data_url(url: str) -> tuple[str, str] | None:
    """Split a base64 data URL into (media type, data)."""
    match = _DATA_URL.match(url)
    if match is None:
        return None
    return match.group("mime"), match.group("data")


def image_url_of(part: dict[str, Any]) -> str:
    image: Any = part.get("image_url")
    if isinstance(image, str):
        return image
    return str(obj(image).get("url", ""))


def parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    """Tool call arguments arrive as a JSON string in the OpenAI format."""
    if not arguments:
        return {}
    value: Any = arguments
    if isinstance(arguments, str):
        try:
            value = json.loads(arguments)
        except ValueError as exc:
            raise UnsupportedRequestError("tool call arguments must be a JSON object") from exc
    result = obj(value)
    if not result and value != {}:
        raise UnsupportedRequestError("tool call arguments must be a JSON object")
    return result


def system_text(messages: list[ChatMessage]) -> str:
    """Join every system and developer message into one system prompt."""
    parts = [text_of(m.content) for m in messages if m.role in ("system", "developer")]
    return "\n\n".join(p for p in parts if p)


def stop_list(stop: str | list[str] | None) -> list[str] | None:
    if stop is None:
        return None
    return [stop] if isinstance(stop, str) else list(stop)


def tool_names_by_id(messages: list[ChatMessage]) -> dict[str, str]:
    """Map each assistant tool call id to its function name."""
    names: dict[str, str] = {}
    for message in messages:
        for call in message.tool_calls or []:
            function = obj(call.get("function"))
            if call.get("id") and function.get("name"):
                names[str(call["id"])] = str(function["name"])
    return names


def function_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The function definitions inside OpenAI tool entries."""
    result: list[dict[str, Any]] = []
    for tool in tools or []:
        if tool.get("type", "function") != "function":
            raise UnsupportedRequestError(f"tool type '{tool.get('type')}' is not supported")
        function = obj(tool.get("function"))
        if not function.get("name"):
            raise UnsupportedRequestError("every tool needs a function name")
        result.append(function)
    return result
