"""Builds the routing policy a route's config names."""

from __future__ import annotations

import random

from prism.config import PolicyName
from prism.core.policies.base import RoutingPolicy
from prism.core.policies.latency import LatencyPolicy, LatencyTracker
from prism.core.policies.priority import PriorityPolicy
from prism.core.policies.round_robin import RoundRobinPolicy
from prism.core.policies.weighted import WeightedPolicy


class PolicyNotAvailableError(ValueError):
    """The config names a policy this version of Prism cannot run yet."""


def make_policy(
    name: PolicyName, *, tracker: LatencyTracker, rng: random.Random | None = None
) -> RoutingPolicy:
    if name is PolicyName.PRIORITY:
        return PriorityPolicy()
    if name is PolicyName.WEIGHTED:
        return WeightedPolicy(rng or random.Random())
    if name is PolicyName.ROUND_ROBIN:
        return RoundRobinPolicy()
    if name is PolicyName.LATENCY:
        return LatencyPolicy(tracker)
    raise PolicyNotAvailableError(
        f"routing policy '{name.value}' is not available yet; "
        "use priority, weighted, round_robin or latency"
    )
