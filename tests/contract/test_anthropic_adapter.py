"""The Anthropic adapter against a pretend Anthropic Messages API."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from prism.providers.anthropic import AnthropicAdapter
from tests.contract.http_harness import HTTPHarness, sse
from tests.contract.suite import AdapterContractSuite, AdapterHarness

MESSAGE_ID = "msg_01TestMessage"

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    503: "api_error",
    529: "overloaded_error",
}


def _event(data: dict[str, Any]) -> str:
    return sse(data, event=str(data["type"]))


def _message(content: list[dict[str, Any]], stop_reason: str | None, usage: dict[str, Any]) -> Any:
    return {
        "id": MESSAGE_ID,
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }


class AnthropicHarness(HTTPHarness):
    def __init__(self) -> None:
        super().__init__()
        self.adapter = AnthropicAdapter("anthropic", client=self.client)

    def success_body(self, text: str, prompt_tokens: int, completion_tokens: int) -> Any:
        return _message(
            [{"type": "text", "text": text}],
            "end_turn",
            {"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
        )

    def stream_start(self) -> list[str]:
        return [
            _event({"type": "message_start", "message": _message([], None, {"output_tokens": 1})}),
            _event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            _event({"type": "ping"}),
        ]

    def stream_piece(self, piece: str) -> list[str]:
        return [
            _event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                }
            )
        ]

    def stream_end(
        self, request: httpx.Request, prompt_tokens: int, completion_tokens: int
    ) -> list[str]:
        return [
            _event({"type": "content_block_stop", "index": 0}),
            _event(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"input_tokens": prompt_tokens, "output_tokens": completion_tokens},
                }
            ),
            _event({"type": "message_stop"}),
        ]

    def error_body(self, status: int) -> Any:
        return {
            "type": "error",
            "error": {"type": _ERROR_TYPES.get(status, "api_error"), "message": f"said {status}"},
        }

    def blocked_response(self) -> tuple[int, Any]:
        return 200, _message([], "refusal", {"input_tokens": 5, "output_tokens": 0})


class TestAnthropicAdapterContract(AdapterContractSuite):
    @pytest.fixture
    def harness(self) -> AdapterHarness:
        return AnthropicHarness()
