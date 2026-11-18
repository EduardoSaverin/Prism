import pytest

from prism.core.errors import GENERIC_UPSTREAM_MESSAGE, POLICIES, ErrorKind, PrismError, policy_for

TABLE = [
    (ErrorKind.RATE_LIMITED, True, True, 429, "rate_limited"),
    (ErrorKind.UPSTREAM_UNAVAILABLE, True, True, 502, "upstream_unavailable"),
    (ErrorKind.INVALID_REQUEST, False, False, 400, "invalid_request"),
    (ErrorKind.AUTH_FAILED, False, True, 502, "upstream_auth_failed"),
    (ErrorKind.CONTENT_BLOCKED, False, False, 400, "content_policy"),
    (ErrorKind.BUDGET_EXCEEDED, False, False, 429, "budget_exceeded"),
    (ErrorKind.INTERNAL_ERROR, False, False, 500, "internal_error"),
]


def test_every_kind_has_a_policy() -> None:
    assert set(POLICIES) == set(ErrorKind)


@pytest.mark.parametrize(("kind", "retry", "next_target", "status", "code"), TABLE)
def test_each_kind_has_the_expected_policy(
    kind: ErrorKind, retry: bool, next_target: bool, status: int, code: str
) -> None:
    policy = policy_for(kind)
    assert policy.retry_same_target is retry
    assert policy.try_next_target is next_target
    assert policy.http_status == status
    assert policy.code == code


def test_a_timeout_is_reported_as_504() -> None:
    error = PrismError(ErrorKind.UPSTREAM_UNAVAILABLE, "slow", timeout=True)
    assert error.http_status == 504
    assert PrismError(ErrorKind.UPSTREAM_UNAVAILABLE, "down").http_status == 502


def test_a_timeout_flag_does_not_change_other_kinds() -> None:
    assert PrismError(ErrorKind.RATE_LIMITED, "x", timeout=True).http_status == 429


def test_auth_failures_hide_the_provider_message() -> None:
    error = PrismError(ErrorKind.AUTH_FAILED, "Invalid API key sk-abc", provider_id="groq")
    body = error.to_openai_error()
    assert body["error"]["message"] == GENERIC_UPSTREAM_MESSAGE
    assert "sk-abc" not in str(body)


def test_invalid_requests_show_the_provider_message() -> None:
    error = PrismError(ErrorKind.INVALID_REQUEST, "temperature must be below 2")
    assert error.to_openai_error() == {
        "error": {
            "message": "temperature must be below 2",
            "type": "invalid_request_error",
            "code": "invalid_request",
        }
    }


def test_retry_after_header_is_at_least_one_second() -> None:
    assert PrismError(ErrorKind.RATE_LIMITED, "x", retry_after_s=0.2).headers() == {
        "Retry-After": "1"
    }
    assert PrismError(ErrorKind.RATE_LIMITED, "x", retry_after_s=12.4).headers() == {
        "Retry-After": "12"
    }
    assert PrismError(ErrorKind.RATE_LIMITED, "x").headers() == {}
