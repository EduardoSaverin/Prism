"""One usage record per request, built from the request context when the request ends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from prism.core.context import RequestContext

STATUS_SUCCESS = "success"
STATUS_CANCELLED = "cancelled"


@dataclass(frozen=True)
class UsageRecord:
    """What one request used and cost. Failed requests are recorded too."""

    ts: datetime
    request_id: str
    route: str
    key_id: str | None
    team_id: str | None
    provider: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int
    # "success", "cancelled" (the client went away), or an error kind such as "rate_limited".
    status: str
    cache_hit: bool
    attempts: int
    budget_threshold: int | None = None


def usage_record(
    ctx: RequestContext,
    status: str,
    *,
    now: datetime | None = None,
    budget_threshold: int | None = None,
) -> UsageRecord:
    """Describe a finished request. Provider and model are those of the last target tried."""
    usage = ctx.usage
    target = ctx.target
    return UsageRecord(
        ts=now or datetime.now(UTC),
        request_id=ctx.request_id,
        route=ctx.route_name,
        key_id=ctx.key_id,
        team_id=ctx.team_id,
        provider=target.provider_id if target else None,
        model=target.model if target else None,
        input_tokens=usage.prompt_tokens if usage else 0,
        output_tokens=usage.completion_tokens if usage else 0,
        cost_usd=ctx.cost_usd if ctx.cost_usd is not None else Decimal(0),
        latency_ms=round(ctx.elapsed_s() * 1000),
        status=status,
        cache_hit=ctx.cache_hit,
        attempts=ctx.attempt,
        budget_threshold=budget_threshold,
    )
