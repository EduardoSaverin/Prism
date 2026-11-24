import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from prism.api.usage import status_of
from prism.core.context import RequestContext
from prism.core.errors import ErrorKind, PrismError
from prism.core.types import Target, Usage
from prism.core.usage.record import usage_record
from tests.scripted import chat_request

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def ctx() -> RequestContext:
    return RequestContext(
        request=chat_request(), route_name="chat", request_id="r1", key_id="k1", team_id="t1"
    )


def test_a_successful_request_records_its_target_tokens_and_cost() -> None:
    c = ctx()
    c.target = Target(provider_id="groq", model="m")
    c.attempt = 2
    c.usage = Usage(prompt_tokens=10, completion_tokens=5)
    c.cost_usd = Decimal("0.00005")
    record = usage_record(c, "success", now=NOW)
    assert (record.ts, record.request_id, record.route) == (NOW, "r1", "chat")
    assert (record.key_id, record.team_id) == ("k1", "t1")
    assert (record.provider, record.model) == ("groq", "m")
    assert (record.input_tokens, record.output_tokens) == (10, 5)
    assert record.cost_usd == Decimal("0.00005")
    assert (record.status, record.cache_hit, record.attempts) == ("success", False, 2)
    assert record.latency_ms >= 0


def test_a_request_that_reached_no_provider_records_zeros() -> None:
    record = usage_record(ctx(), "route_not_found")
    assert (record.provider, record.model) == (None, None)
    assert (record.input_tokens, record.output_tokens, record.cost_usd) == (0, 0, 0)
    assert (record.status, record.attempts) == ("route_not_found", 0)


def test_a_plugin_answer_is_recorded_as_a_cache_hit() -> None:
    c = ctx()
    c.cache_hit = True
    assert usage_record(c, "success").cache_hit


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (None, "success"),
        (PrismError(ErrorKind.RATE_LIMITED, "slow down"), "rate_limited"),
        (RuntimeError("bug"), "internal_error"),
        (asyncio.CancelledError(), "cancelled"),
        (GeneratorExit(), "cancelled"),
    ],
)
def test_how_a_request_ended_becomes_its_status(error: BaseException | None, status: str) -> None:
    assert status_of(error) == status
