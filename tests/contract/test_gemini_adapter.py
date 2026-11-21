"""The Gemini adapter against a pretend Gemini generateContent API."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from prism.providers.gemini import GeminiAdapter
from tests.contract.http_harness import HTTPHarness, sse
from tests.contract.suite import AdapterContractSuite, AdapterHarness

_STATUSES = {
    400: "INVALID_ARGUMENT",
    401: "UNAUTHENTICATED",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    422: "INVALID_ARGUMENT",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    503: "UNAVAILABLE",
}


def _candidate(text: str, finish_reason: str | None = None) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "content": {"role": "model", "parts": [{"text": text}]},
        "index": 0,
    }
    if finish_reason:
        candidate["finishReason"] = finish_reason
    return candidate


def _usage(prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    return {
        "promptTokenCount": prompt_tokens,
        "candidatesTokenCount": completion_tokens,
        "totalTokenCount": prompt_tokens + completion_tokens,
    }


class GeminiHarness(HTTPHarness):
    def __init__(self) -> None:
        super().__init__()
        self.adapter = GeminiAdapter("gemini", client=self.client)

    def success_body(self, text: str, prompt_tokens: int, completion_tokens: int) -> Any:
        return {
            "candidates": [_candidate(text, "STOP")],
            "usageMetadata": _usage(prompt_tokens, completion_tokens),
            "modelVersion": "gemini-test",
            "responseId": "resp-123",
        }

    def stream_piece(self, piece: str) -> list[str]:
        return [
            sse(
                {
                    "candidates": [_candidate(piece)],
                    "usageMetadata": {"promptTokenCount": 1},
                    "modelVersion": "gemini-test",
                }
            )
        ]

    def stream_end(
        self, request: httpx.Request, prompt_tokens: int, completion_tokens: int
    ) -> list[str]:
        return [
            sse(
                {
                    "candidates": [_candidate("", "STOP")],
                    "usageMetadata": _usage(prompt_tokens, completion_tokens),
                    "modelVersion": "gemini-test",
                }
            )
        ]

    def error_body(self, status: int) -> Any:
        return {
            "error": {
                "code": status,
                "message": f"upstream said {status}",
                "status": _STATUSES.get(status, "UNKNOWN"),
            }
        }

    def blocked_response(self) -> tuple[int, Any]:
        return 200, {
            "promptFeedback": {"blockReason": "SAFETY", "safetyRatings": []},
            "usageMetadata": {"promptTokenCount": 5, "totalTokenCount": 5},
            "modelVersion": "gemini-test",
        }


class TestGeminiAdapterContract(AdapterContractSuite):
    @pytest.fixture
    def harness(self) -> AdapterHarness:
        return GeminiHarness()
