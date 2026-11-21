"""Internal request and response types.

Prism uses the OpenAI chat format as its own format. Every provider adapter
converts to and from this format, so the rest of the gateway only ever sees
one shape.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Role = Literal["system", "developer", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "function_call"]


def new_id(prefix: str) -> str:
    """Make a short unique id such as "chatcmpl-3f2a9c..."."""
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


class ChatMessage(BaseModel):
    """One message in a conversation."""

    model_config = ConfigDict(extra="allow")

    role: Role
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ChatRequest(BaseModel):
    """A chat completion request, in OpenAI format."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "examples": [
                {
                    "model": "fast-chat",
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."},
                        {"role": "user", "content": "Hello!"},
                    ],
                    "stream": False,
                }
            ]
        },
    )

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    n: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    seed: int | None = None
    user: str | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    response_format: dict[str, Any] | None = None
    stream_options: dict[str, Any] | None = None

    def extra_params(self) -> dict[str, Any]:
        """Return the fields Prism does not know about."""
        return dict(self.model_extra or {})

    @property
    def wants_stream_usage(self) -> bool:
        """True when the caller asked for token usage in the last stream chunk."""
        return bool(self.stream_options and self.stream_options.get("include_usage"))


class Usage(BaseModel):
    """Token counts for one request."""

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _fill_total(self) -> Self:
        if self.total_tokens == 0:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: FinishReason | None = "stop"


class ChatResponse(BaseModel):
    """A full (non-streaming) answer, in OpenAI format."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: new_id("chatcmpl"))
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage | None = None

    @property
    def text(self) -> str:
        """The text of the first choice, or an empty string."""
        if not self.choices:
            return ""
        content = self.choices[0].message.content
        return content if isinstance(content, str) else ""


class Delta(BaseModel):
    """The new piece of a message carried by one stream chunk."""

    model_config = ConfigDict(extra="allow")

    role: Role | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChunkChoice(BaseModel):
    index: int = 0
    delta: Delta = Field(default_factory=Delta)
    finish_reason: FinishReason | None = None


class ChatChunk(BaseModel):
    """One piece of a streamed answer, in OpenAI format."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: new_id("chatcmpl"))
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChunkChoice] = Field(default_factory=lambda: [])
    usage: Usage | None = None

    @property
    def text(self) -> str:
        """The text carried by this chunk, or an empty string."""
        if not self.choices:
            return ""
        return self.choices[0].delta.content or ""

    @property
    def finish_reason(self) -> FinishReason | None:
        if not self.choices:
            return None
        return self.choices[0].finish_reason


class Target(BaseModel, frozen=True):
    """One place a route can send traffic: a provider, a model and its keys."""

    provider_id: str
    model: str
    # The provider keys this target may use. Empty means all of them.
    key_ids: tuple[str, ...] = ()
    weight: int = Field(default=1, ge=1)


class ResolvedKey(BaseModel, frozen=True):
    """The key the router picked for one call. The secret never shows in logs."""

    id: str
    secret: SecretStr
