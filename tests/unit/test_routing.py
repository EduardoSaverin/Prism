import random

import pytest
from pydantic import SecretStr

from prism.config import PolicyName, ProviderConfig
from prism.core.context import RequestContext
from prism.core.keys import KeyPool, KeyState, NoUsableKeyError, build_key_pool, key_id
from prism.core.policies.base import RoutingPolicy
from prism.core.policies.latency import LatencyPolicy, LatencyTracker
from prism.core.policies.priority import PriorityPolicy
from prism.core.policies.registry import PolicyNotAvailableError, make_policy
from prism.core.policies.round_robin import RoundRobinPolicy
from prism.core.policies.weighted import WeightedPolicy
from prism.core.resilience import BreakerRegistry, BreakerState, CircuitBreaker, backoff_delay
from prism.core.types import ChatMessage, ChatRequest, Target
from tests.scripted import FakeClock

CTX = RequestContext(
    request=ChatRequest(model="r", messages=[ChatMessage(role="user", content="x")]),
    route_name="r",
)
A = Target(provider_id="a", model="m", weight=1)
B = Target(provider_id="b", model="m", weight=1)
C = Target(provider_id="c", model="m", weight=1)


@pytest.mark.parametrize("attempt", [0, 1, 2, 3, 10])
def test_backoff_grows_is_capped_and_jittered(attempt: int) -> None:
    rng = random.Random(7)
    ceiling = min(2.0, 0.2 * 2**attempt)
    for _ in range(50):
        delay = backoff_delay(attempt, 0.2, rng=rng)
        assert ceiling * 0.5 <= delay <= ceiling


def test_backoff_with_no_base_never_waits() -> None:
    assert backoff_delay(3, 0) == 0.0


def test_breaker_opens_after_the_threshold() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=3, cooldown_s=10, clock=clock)
    for _ in range(2):
        breaker.record_failure()
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allow()


def test_a_success_resets_the_failure_count() -> None:
    breaker = CircuitBreaker(failure_threshold=2, clock=FakeClock())
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED


def test_half_open_lets_one_probe_through() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=10, clock=clock)
    breaker.record_failure()
    clock.advance(10)
    assert breaker.allow()
    assert breaker.state is BreakerState.HALF_OPEN
    assert not breaker.allow()
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED


def test_a_failed_probe_opens_the_breaker_again() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=10, clock=clock)
    breaker.record_failure()
    clock.advance(10)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    clock.advance(5)
    assert not breaker.allow()


def test_a_released_probe_lets_the_next_request_try() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_s=1, clock=clock)
    breaker.record_failure()
    clock.advance(1)
    assert breaker.allow()
    breaker.release()
    assert breaker.allow()


def test_registry_keeps_one_breaker_per_target() -> None:
    registry = BreakerRegistry(clock=FakeClock())
    assert registry.get("a", "m") is registry.get("a", "m")
    assert registry.get("a", "m") is not registry.get("a", "n")
    assert registry.states() == {("a", "m"): BreakerState.CLOSED, ("a", "n"): BreakerState.CLOSED}


def _pool(*weights: int, clock: FakeClock | None = None) -> KeyPool:
    keys = [KeyState(f"k{i}", SecretStr(f"s{i}"), w) for i, w in enumerate(weights)]
    return KeyPool("p", keys, clock=clock or FakeClock(), rng=random.Random(3))


def test_keys_are_picked_by_weight() -> None:
    pool = _pool(3, 1)
    picks = [pool.pick() for _ in range(2000)]
    share = sum(1 for key in picks if key is not None and key.id == "k0") / len(picks)
    assert 0.7 < share < 0.8


def test_a_disabled_key_is_never_picked() -> None:
    pool = _pool(1, 1)
    pool.disable("k0")
    assert {key.id for key in (pool.pick() for _ in range(50)) if key} == {"k1"}


def test_a_cooling_key_comes_back_after_its_wait() -> None:
    clock = FakeClock()
    pool = _pool(1, clock=clock)
    pool.cool_down("k0", 5)
    with pytest.raises(NoUsableKeyError) as caught:
        pool.pick()
    assert caught.value.retry_after_s == 5
    clock.advance(5)
    assert pool.pick() is not None


def test_a_pool_with_only_disabled_keys_has_no_retry_time() -> None:
    pool = _pool(1)
    pool.disable("k0")
    with pytest.raises(NoUsableKeyError) as caught:
        pool.pick()
    assert caught.value.retry_after_s is None


def test_pick_can_be_limited_to_some_keys() -> None:
    pool = _pool(1, 1, 1)
    picks = {key.id for key in (pool.pick(["k1", "k2"]) for _ in range(200)) if key}
    assert picks == {"k1", "k2"}
    assert {key.id for key in (pool.pick() for _ in range(200)) if key} == {"k0", "k1", "k2"}


def test_limited_keys_share_health_with_the_whole_pool() -> None:
    clock = FakeClock()
    pool = _pool(1, 1, clock=clock)
    pool.cool_down("k1", 4)
    with pytest.raises(NoUsableKeyError) as caught:
        pool.pick(["k1"])
    assert caught.value.retry_after_s == 4  # only k1's wait counts, not k0's
    assert pool.pick() is not None  # k0 is still usable for targets that may use it
    pool.disable("k0")
    with pytest.raises(NoUsableKeyError) as disabled:
        pool.pick(["k0"])
    assert disabled.value.retry_after_s is None


def test_a_key_id_names_the_provider_and_the_variable() -> None:
    assert key_id("groq", "GROQ_KEY_1") == "groq/GROQ_KEY_1"


def test_keys_are_read_from_the_environment_and_unset_ones_skipped() -> None:
    provider = ProviderConfig.model_validate(
        {"id": "groq", "kind": "groq", "keys": [{"env": "SET", "weight": 2}, {"env": "UNSET"}]}
    )
    pool = build_key_pool(provider, {"SET": "value"}, keyless=False)
    assert [(k.id, k.weight) for k in pool.keys] == [("groq/SET", 2)]
    key = pool.pick()
    assert key is not None
    assert key.secret.get_secret_value() == "value"


def test_a_keyless_provider_gets_no_key() -> None:
    provider = ProviderConfig.model_validate({"id": "local", "kind": "ollama"})
    assert build_key_pool(provider, {}, keyless=True).pick() is None


def test_priority_keeps_the_route_order() -> None:
    assert PriorityPolicy().order([A, B, C], CTX) == [A, B, C]


def test_round_robin_rotates_the_start() -> None:
    policy = RoundRobinPolicy()
    firsts = [policy.order([A, B, C], CTX)[0] for _ in range(4)]
    assert firsts == [A, B, C, A]
    assert policy.order([], CTX) == []


def test_weighted_keeps_every_target_and_favours_heavy_ones() -> None:
    heavy = Target(provider_id="heavy", model="m", weight=9)
    policy = WeightedPolicy(random.Random(5))
    orders = [policy.order([A, heavy], CTX) for _ in range(1000)]
    assert all(sorted(t.provider_id for t in o) == ["a", "heavy"] for o in orders)
    share = sum(1 for o in orders if o[0] is heavy) / len(orders)
    assert 0.85 < share < 0.95


def test_latency_tries_unmeasured_then_fastest_targets_first() -> None:
    tracker = LatencyTracker(smoothing=0.5)
    tracker.record(A, 0.9)
    tracker.record(B, 0.2)
    assert LatencyPolicy(tracker).order([A, B, C], CTX) == [C, B, A]
    tracker.record(B, 2.0)
    assert tracker.average(B) == pytest.approx(1.1)
    assert LatencyPolicy(tracker).order([A, B], CTX) == [A, B]


@pytest.mark.parametrize(
    "name", [PolicyName.PRIORITY, PolicyName.WEIGHTED, PolicyName.ROUND_ROBIN, PolicyName.LATENCY]
)
def test_built_in_policies_follow_the_contract(name: PolicyName) -> None:
    policy = make_policy(name, tracker=LatencyTracker())
    assert isinstance(policy, RoutingPolicy)
    assert policy.name == name.value


@pytest.mark.parametrize("name", [PolicyName.COST, PolicyName.QUOTA])
def test_policies_that_need_later_phases_are_refused(name: PolicyName) -> None:
    with pytest.raises(PolicyNotAvailableError, match=name.value):
        make_policy(name, tracker=LatencyTracker())
