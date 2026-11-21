"""The request pipeline: route, call the provider, retry or fall back.

For each request the gateway resolves the route, runs plugins, orders the
route's targets with its routing policy, and tries them one by one. Each failure
is classified by the adapter and handled by the error table in `core.errors`:
retry the same target with backoff, move to the next target, or stop.

Streams follow the same rules until the first chunk arrives. After that the
caller has already received part of an answer, so a failure ends the stream.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TypeVar

from prism.config import PrismConfig, RouteConfig
from prism.core.context import RequestContext
from prism.core.errors import ErrorKind, PrismError
from prism.core.keys import KeyPool, NoUsableKeyError, key_id
from prism.core.policies.base import RoutingPolicy
from prism.core.policies.latency import LatencyTracker
from prism.core.policies.registry import make_policy
from prism.core.resilience import (
    BreakerRegistry,
    BreakerState,
    CircuitBreaker,
    Clock,
    backoff_delay,
)
from prism.core.types import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    ChunkChoice,
    Delta,
    ResolvedKey,
    Target,
)
from prism.plugins.base import Plugin, Reject, ShortCircuit
from prism.providers.base import ProviderAdapter

log = logging.getLogger(__name__)

T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]

DEFAULT_MAX_RETRY_WAIT_S = 5.0
_BREAKER_KINDS = {ErrorKind.RATE_LIMITED, ErrorKind.UPSTREAM_UNAVAILABLE}
_INTERNAL_MESSAGE = "An internal error occurred in the gateway."


@dataclass(frozen=True)
class RouteRuntime:
    config: RouteConfig
    targets: tuple[Target, ...]
    policy: RoutingPolicy


@dataclass
class CallResult:
    response: ChatResponse
    target: Target | None
    attempts: int


@dataclass
class StreamResult:
    chunks: AsyncIterator[ChatChunk]
    target: Target | None
    attempts: int


@dataclass
class _Attempts:
    count: int = 0
    last_error: PrismError | None = None
    rate_limit_waits: list[float] = field(default_factory=lambda: [])

    def record(self, error: PrismError) -> None:
        self.last_error = error
        if error.kind is ErrorKind.RATE_LIMITED and error.retry_after_s is not None:
            self.rate_limit_waits.append(error.retry_after_s)


class Gateway:
    def __init__(
        self,
        config: PrismConfig,
        adapters: Mapping[str, ProviderAdapter],
        pools: Mapping[str, KeyPool],
        *,
        breakers: BreakerRegistry | None = None,
        tracker: LatencyTracker | None = None,
        plugins: Sequence[Plugin] = (),
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
        max_retry_wait_s: float = DEFAULT_MAX_RETRY_WAIT_S,
    ) -> None:
        self.config = config
        self.adapters = dict(adapters)
        self.pools = dict(pools)
        self.breakers = breakers or BreakerRegistry(
            config.circuit_breaker.failure_threshold, config.circuit_breaker.cooldown_s, clock
        )
        self.tracker = tracker or LatencyTracker()
        self.plugins = tuple(plugins)
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.max_retry_wait_s = max_retry_wait_s
        self.routes = {route.name: self._build_route(route) for route in config.routes}

    def _build_route(self, route: RouteConfig) -> RouteRuntime:
        targets = tuple(
            Target(
                provider_id=t.provider,
                model=t.model,
                key_ids=tuple(key_id(t.provider, env) for env in t.keys),
                weight=t.weight,
            )
            for t in route.targets
        )
        policy = make_policy(route.policy, tracker=self.tracker, rng=self.rng)
        return RouteRuntime(route, targets, policy)

    def route_names(self) -> list[str]:
        return list(self.routes)

    async def aclose(self) -> None:
        for adapter in self.adapters.values():
            close = getattr(adapter, "aclose", None)
            if close is not None:
                await close()

    def resolve(self, model: str) -> RouteRuntime:
        route = self.routes.get(model)
        if route is None:
            raise PrismError(
                ErrorKind.ROUTE_NOT_FOUND,
                f"The model '{model}' does not exist. Call GET /v1/models to list the routes.",
            )
        return route

    async def chat(self, req: ChatRequest, ctx: RequestContext) -> CallResult:
        route = self.resolve(req.model)
        short = await self._before(ctx)
        if short is not None:
            return CallResult(short, None, 0)

        async def call(adapter: ProviderAdapter, target: Target, key: ResolvedKey | None):
            return await adapter.chat(req, target, key)

        response, target, attempts = await self._run(route, ctx, call)
        self._record_success(target, ctx)
        response.model = req.model
        for plugin in self.plugins:
            await plugin.after_response(ctx, response)
        return CallResult(response, target, attempts)

    async def stream(self, req: ChatRequest, ctx: RequestContext) -> StreamResult:
        route = self.resolve(req.model)
        short = await self._before(ctx)
        if short is not None:
            return StreamResult(_response_as_chunks(short, req.wants_stream_usage), None, 0)

        upstream_req = req.model_copy(
            update={"stream": True, "stream_options": {"include_usage": True}}
        )

        async def call(adapter: ProviderAdapter, target: Target, key: ResolvedKey | None):
            iterator = adapter.chat_stream(upstream_req, target, key)
            try:
                first = await anext(iterator)
            except StopAsyncIteration as exc:
                raise PrismError(
                    ErrorKind.UPSTREAM_UNAVAILABLE,
                    "The provider closed the stream without sending anything.",
                    provider_id=target.provider_id,
                ) from exc
            except BaseException:
                await _close(iterator)
                raise
            return first, iterator

        (first, iterator), target, attempts = await self._run(route, ctx, call)
        breaker = self.breakers.get(target.provider_id, target.model)
        breaker.record_success()
        chunks = self._relay(first, iterator, target, req, ctx)
        return StreamResult(chunks, target, attempts)

    async def _relay(
        self,
        first: ChatChunk,
        iterator: AsyncIterator[ChatChunk],
        target: Target,
        req: ChatRequest,
        ctx: RequestContext,
    ) -> AsyncIterator[ChatChunk]:
        adapter = self.adapters[target.provider_id]
        want_usage = req.wants_stream_usage
        try:
            async for chunk in _prepend(first, iterator):
                if chunk.usage is not None:
                    ctx.metadata["usage"] = chunk.usage
                    if not want_usage:
                        chunk.usage = None
                chunk.model = req.model
                yield chunk
        except PrismError:
            raise
        except Exception as exc:
            error = self._to_prism_error(adapter, exc, target)
            if error.kind in _BREAKER_KINDS:
                self.breakers.get(target.provider_id, target.model).record_failure()
            log.warning(
                "Stream from %s failed after it started: %s", target.provider_id, error.message
            )
            raise error from exc
        finally:
            await _close(iterator)
        self.tracker.record(target, ctx.elapsed_s())

    async def _before(self, ctx: RequestContext) -> ChatResponse | None:
        for plugin in self.plugins:
            decision = await plugin.before_request(ctx)
            if isinstance(decision, Reject):
                raise decision.error
            if isinstance(decision, ShortCircuit):
                return decision.response
        return None

    async def _run(
        self,
        route: RouteRuntime,
        ctx: RequestContext,
        call: Callable[[ProviderAdapter, Target, ResolvedKey | None], Awaitable[T]],
    ) -> tuple[T, Target, int]:
        attempts = _Attempts()
        max_retries = route.config.retry.max_retries
        base_s = route.config.retry.backoff_ms / 1000

        for target in route.policy.order(route.targets, ctx):
            adapter = self.adapters.get(target.provider_id)
            pool = self.pools.get(target.provider_id)
            if adapter is None or pool is None:
                continue
            breaker = self.breakers.get(target.provider_id, target.model)

            for retry in range(max_retries + 1):
                if not breaker.allow():
                    attempts.record(
                        PrismError(
                            ErrorKind.UPSTREAM_UNAVAILABLE,
                            f"Circuit breaker is open for {target.provider_id}/{target.model}.",
                            provider_id=target.provider_id,
                        )
                    )
                    break
                try:
                    key = pool.pick(target.key_ids)
                except NoUsableKeyError as exc:
                    breaker.release()
                    kind = (
                        ErrorKind.RATE_LIMITED
                        if exc.retry_after_s is not None
                        else ErrorKind.UPSTREAM_UNAVAILABLE
                    )
                    attempts.record(
                        PrismError(
                            kind,
                            str(exc),
                            provider_id=target.provider_id,
                            retry_after_s=exc.retry_after_s,
                        )
                    )
                    break

                attempts.count += 1
                ctx.attempt = attempts.count
                ctx.metadata["provider"] = target.provider_id
                started = self.clock()
                try:
                    result = await call(adapter, target, key)
                except asyncio.CancelledError:
                    breaker.release()
                    raise
                except Exception as exc:
                    error = self._to_prism_error(adapter, exc, target)
                    attempts.record(error)
                    self._after_failure(error, breaker, pool, key, target)
                    log.info(
                        "Attempt %d on %s/%s failed: %s (%s)",
                        attempts.count,
                        target.provider_id,
                        target.model,
                        error.kind.value,
                        error.message,
                    )
                    if not self._should_retry(error, retry, max_retries, breaker):
                        if self._should_try_next(error, route):
                            break
                        raise error from exc
                    await self.sleep(self._retry_delay(error, retry, base_s))
                    continue
                else:
                    ctx.metadata["upstream_latency_s"] = self.clock() - started
                    return result, target, attempts.count

        raise self._final_error(route, attempts)

    def _to_prism_error(
        self, adapter: ProviderAdapter, exc: Exception, target: Target
    ) -> PrismError:
        if isinstance(exc, PrismError):
            return exc
        kind = adapter.classify_error(exc)
        if kind is ErrorKind.INTERNAL_ERROR:
            log.exception("Adapter %s raised an unexpected error", target.provider_id)
            return PrismError(kind, _INTERNAL_MESSAGE, provider_id=target.provider_id)
        message = getattr(exc, "message", None) or str(exc) or exc.__class__.__name__
        return PrismError(
            kind,
            str(message),
            provider_id=target.provider_id,
            retry_after_s=getattr(exc, "retry_after_s", None),
            timeout="timed out" in str(exc) or "Timeout" in exc.__class__.__name__,
        )

    def _after_failure(
        self,
        error: PrismError,
        breaker: CircuitBreaker,
        pool: KeyPool,
        key: ResolvedKey | None,
        target: Target,
    ) -> None:
        if error.kind in _BREAKER_KINDS:
            breaker.record_failure()
        else:
            breaker.release()
        if key is None:
            return
        if error.kind is ErrorKind.AUTH_FAILED:
            pool.disable(key.id)
            log.error("Provider %s rejected key %s", target.provider_id, key.id)
        elif error.kind is ErrorKind.RATE_LIMITED:
            pool.cool_down(key.id, error.retry_after_s)

    def _should_retry(
        self, error: PrismError, retry: int, max_retries: int, breaker: CircuitBreaker
    ) -> bool:
        if not error.policy.retry_same_target or retry >= max_retries:
            return False
        if breaker.state is not BreakerState.CLOSED:
            return False
        return error.retry_after_s is None or error.retry_after_s <= self.max_retry_wait_s

    @staticmethod
    def _should_try_next(error: PrismError, route: RouteRuntime) -> bool:
        if error.kind is ErrorKind.CONTENT_BLOCKED:
            return route.config.fallback_on_content_blocked
        return error.policy.try_next_target

    def _retry_delay(self, error: PrismError, retry: int, base_s: float) -> float:
        delay = backoff_delay(retry, base_s, rng=self.rng)
        if error.retry_after_s is not None:
            delay = max(delay, error.retry_after_s)
        return delay

    @staticmethod
    def _final_error(route: RouteRuntime, attempts: _Attempts) -> PrismError:
        error = attempts.last_error
        if error is None:
            return PrismError(
                ErrorKind.UPSTREAM_UNAVAILABLE,
                f"No provider is available for route '{route.config.name}'.",
            )
        if error.kind is ErrorKind.RATE_LIMITED and attempts.rate_limit_waits:
            return PrismError(
                error.kind,
                error.message,
                provider_id=error.provider_id,
                retry_after_s=min(attempts.rate_limit_waits),
            )
        return error

    def _record_success(self, target: Target, ctx: RequestContext) -> None:
        self.breakers.get(target.provider_id, target.model).record_success()
        latency = ctx.metadata.get("upstream_latency_s")
        if isinstance(latency, float):
            self.tracker.record(target, latency)


async def _close(iterator: AsyncIterator[ChatChunk]) -> None:
    close = getattr(iterator, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception:
            log.debug("Ignoring an error while closing an upstream stream", exc_info=True)


async def _prepend(first: ChatChunk, rest: AsyncIterator[ChatChunk]) -> AsyncIterator[ChatChunk]:
    yield first
    async for chunk in rest:
        yield chunk


async def _response_as_chunks(resp: ChatResponse, with_usage: bool) -> AsyncIterator[ChatChunk]:
    for choice in resp.choices:
        message = choice.message
        yield ChatChunk(
            id=resp.id,
            model=resp.model,
            choices=[
                ChunkChoice(
                    index=choice.index,
                    delta=Delta(
                        role="assistant",
                        content=message.content if isinstance(message.content, str) else None,
                        tool_calls=message.tool_calls,
                    ),
                )
            ],
        )
    last = resp.choices[-1] if resp.choices else None
    yield ChatChunk(
        id=resp.id,
        model=resp.model,
        choices=[
            ChunkChoice(
                index=last.index if last else 0,
                finish_reason=(last.finish_reason if last else None) or "stop",
            )
        ],
        usage=resp.usage if with_usage else None,
    )
