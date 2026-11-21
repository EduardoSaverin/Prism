"""The OpenAI-compatible adapter against a pretend OpenAI chat completions API."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from prism.config import ProviderKind
from prism.providers.openai_compat import OpenAICompatAdapter
from prism.providers.registry import PRESETS
from tests.contract.http_harness import HTTPHarness, sse
from tests.contract.suite import AdapterContractSuite, AdapterHarness

CHUNK_ID = "chatcmpl-abc123"


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": CHUNK_ID,
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "gpt-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


class OpenAIHarness(HTTPHarness):
    def __init__(self) -> None:
        super().__init__()
        self.adapter = OpenAICompatAdapter(
            "openai", PRESETS[ProviderKind.OPENAI], client=self.client
        )

    def success_body(self, text: str, prompt_tokens: int, completion_tokens: int) -> Any:
        return {
            "id": CHUNK_ID,
            "object": "chat.completion",
            "created": 1700000000,
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text, "refusal": None},
                    "logprobs": None,
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "system_fingerprint": "fp_test",
        }

    def stream_start(self) -> list[str]:
        return [sse(_chunk({"role": "assistant", "content": ""}))]

    def stream_piece(self, piece: str) -> list[str]:
        return [sse(_chunk({"content": piece}))]

    def stream_end(
        self, request: httpx.Request, prompt_tokens: int, completion_tokens: int
    ) -> list[str]:
        events = [sse(_chunk({}, "stop"))]
        options = json.loads(request.content).get("stream_options") or {}
        if options.get("include_usage"):
            usage_chunk = _chunk({})
            usage_chunk["choices"] = []
            usage_chunk["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            events.append(sse(usage_chunk))
        return [*events, sse("[DONE]")]

    def error_body(self, status: int) -> Any:
        kind = {429: "rate_limit_exceeded", 401: "invalid_api_key"}.get(status, "server_error")
        return {"error": {"message": f"upstream said {status}", "type": kind, "code": None}}

    def blocked_response(self) -> tuple[int, Any]:
        return 400, {
            "error": {
                "message": "The response was filtered due to the prompt triggering a policy.",
                "type": "invalid_request_error",
                "param": "prompt",
                "code": "content_filter",
            }
        }


class TestOpenAICompatAdapterContract(AdapterContractSuite):
    @pytest.fixture
    def harness(self) -> AdapterHarness:
        return OpenAIHarness()
