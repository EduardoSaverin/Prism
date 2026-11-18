"""Error kinds and how Prism reacts to each one.

Every failure, from any provider, is turned into one of these kinds. The
router then follows the table below: retry, move to the next target, or
return a clean error to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    INVALID_REQUEST = "invalid_request"
    AUTH_FAILED = "auth_failed"
    CONTENT_BLOCKED = "content_blocked"
    BUDGET_EXCEEDED = "budget_exceeded"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class ErrorPolicy:
    """What Prism does when an error of one kind happens."""

    retry_same_target: bool
    try_next_target: bool
    http_status: int
    openai_type: str
    code: str
    hide_provider_message: bool = False


POLICIES: dict[ErrorKind, ErrorPolicy] = {
    ErrorKind.RATE_LIMITED: ErrorPolicy(True, True, 429, "rate_limit_error", "rate_limited"),
    ErrorKind.UPSTREAM_UNAVAILABLE: ErrorPolicy(
        True, True, 502, "upstream_error", "upstream_unavailable"
    ),
    ErrorKind.INVALID_REQUEST: ErrorPolicy(
        False, False, 400, "invalid_request_error", "invalid_request"
    ),
    ErrorKind.AUTH_FAILED: ErrorPolicy(
        False, True, 502, "upstream_error", "upstream_auth_failed", hide_provider_message=True
    ),
    ErrorKind.CONTENT_BLOCKED: ErrorPolicy(
        False, False, 400, "invalid_request_error", "content_policy"
    ),
    ErrorKind.BUDGET_EXCEEDED: ErrorPolicy(
        False, False, 429, "rate_limit_error", "budget_exceeded"
    ),
    ErrorKind.INTERNAL_ERROR: ErrorPolicy(False, False, 500, "internal_error", "internal_error"),
}

GENERIC_UPSTREAM_MESSAGE = "The upstream provider returned an error."


def policy_for(kind: ErrorKind) -> ErrorPolicy:
    return POLICIES[kind]


class PrismError(Exception):
    """An error with a kind, so the gateway knows what to do with it."""

    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        *,
        provider_id: str | None = None,
        retry_after_s: float | None = None,
        timeout: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.provider_id = provider_id
        self.retry_after_s = retry_after_s
        self.timeout = timeout

    @property
    def policy(self) -> ErrorPolicy:
        return POLICIES[self.kind]

    @property
    def http_status(self) -> int:
        if self.kind is ErrorKind.UPSTREAM_UNAVAILABLE and self.timeout:
            return 504
        return self.policy.http_status

    @property
    def client_message(self) -> str:
        if self.policy.hide_provider_message:
            return GENERIC_UPSTREAM_MESSAGE
        return self.message

    def headers(self) -> dict[str, str]:
        """Extra response headers, such as Retry-After."""
        if self.retry_after_s is not None:
            return {"Retry-After": str(max(1, round(self.retry_after_s)))}
        return {}

    def to_openai_error(self) -> dict[str, Any]:
        """The JSON body sent to the caller, in the OpenAI error shape."""
        return {
            "error": {
                "message": self.client_message,
                "type": self.policy.openai_type,
                "code": self.policy.code,
            }
        }
