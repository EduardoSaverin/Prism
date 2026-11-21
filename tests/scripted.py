"""Test doubles for the gateway: a scripted adapter, a fake clock and a builder."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from prism.config import parse_config
from prism.core.errors import ErrorKind
from prism.core.gateway import Gateway
from prism.core.keys import KeyPool, build_key_pool
from prism.core.resilience import BreakerRegistry
from prism.core.types import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Choice,
    ChunkChoice,
    Delta,
    ResolvedKey,
    Target,
    Usage,
)
from prism.providers.fake import FakeAdapter, FakeProviderError


class RetryAfterError(FakeProviderError):
    def __init__(self, status: int, retry_after_s: float) -> None:
        super().__init__(status, f"HTTP {status}")
        self.retry_after_s = retry_after_s


@dataclass
class Call:
    request: ChatRequest
    target: Target
    key: ResolvedKey | None


class ScriptedAdapter:
    """Raises the queued errors one per call, then answers normally."""

    def __init__(
        self,
        name: str,
        *,
        errors: Sequence[Exception] | None = None,
        reply: str = "ok",
        pieces: list[str] | None = None,
        break_stream_after: int | None = None,
    ) -> None:
        self.name = name
        self.errors = list(errors or [])
        self.reply = reply
        self.pieces = pieces
        self.break_stream_after = break_stream_after
        self.calls: list[Call] = []
        self.closed = False

    async def chat(self, req: ChatRequest, target: Target, key: ResolvedKey | None) -> ChatResponse:
        self.calls.append(Call(req, target, key))
        if self.errors:
            raise self.errors.pop(0)
        return ChatResponse(
            model=target.model,
            choices=[Choice(message=ChatMessage(role="assistant", content=self.reply))],
            usage=Usage(prompt_tokens=10, completion_tokens=5),
        )

    async def chat_stream(
        self, req: ChatRequest, target: Target, key: ResolvedKey | None
    ) -> AsyncIterator[ChatChunk]:
        self.calls.append(Call(req, target, key))
        if self.errors:
            raise self.errors.pop(0)
        yield ChatChunk(model=target.model, choices=[ChunkChoice(delta=Delta(role="assistant"))])
        for number, piece in enumerate(self.pieces or [self.reply]):
            if self.break_stream_after is not None and number >= self.break_stream_after:
                raise FakeProviderError(None, "connection dropped")
            yield ChatChunk(model=target.model, choices=[ChunkChoice(delta=Delta(content=piece))])
        usage = Usage(prompt_tokens=10, completion_tokens=5) if req.wants_stream_usage else None
        yield ChatChunk(
            model=target.model, choices=[ChunkChoice(finish_reason="stop")], usage=usage
        )

    def classify_error(self, exc: Exception) -> ErrorKind:
        return FakeAdapter().classify_error(exc)

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Sleeps:
    delays: list[float] = field(default_factory=lambda: [])

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def route(name: str, *providers: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "targets": [{"provider": p, "model": f"{p}-model"} for p in providers],
        **extra,
    }


def config_for(
    providers: list[str],
    routes: list[dict[str, Any]],
    *,
    keys: Mapping[str, list[str]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    provider_entries: list[dict[str, Any]] = []
    for provider in providers:
        names = (keys or {}).get(provider)
        if names:
            provider_entries.append(
                {"id": provider, "kind": "groq", "keys": [{"env": n} for n in names]}
            )
        else:
            provider_entries.append({"id": provider, "kind": "ollama"})
    return {"providers": provider_entries, "routes": routes, **extra}


@dataclass
class Harness:
    gateway: Gateway
    adapters: dict[str, ScriptedAdapter]
    clock: FakeClock
    sleeps: Sleeps
    pools: dict[str, KeyPool]


def build(
    config: dict[str, Any],
    adapters: Mapping[str, ScriptedAdapter],
    *,
    env: Mapping[str, str] | None = None,
    seed: int = 1,
    max_retry_wait_s: float = 5.0,
) -> Harness:
    parsed = parse_config(config)
    clock = FakeClock()
    sleeps = Sleeps()
    rng = random.Random(seed)
    pools = {
        p.id: build_key_pool(p, env or {}, keyless=p.kind.value == "ollama", clock=clock, rng=rng)
        for p in parsed.providers
    }
    breakers = BreakerRegistry(
        parsed.circuit_breaker.failure_threshold, parsed.circuit_breaker.cooldown_s, clock
    )
    gateway = Gateway(
        parsed,
        adapters,
        pools,
        breakers=breakers,
        sleep=sleeps,
        clock=clock,
        rng=rng,
        max_retry_wait_s=max_retry_wait_s,
    )
    return Harness(gateway, dict(adapters), clock, sleeps, pools)


def chat_request(model: str = "chat", *, stream: bool = False, usage: bool = False) -> ChatRequest:
    return ChatRequest(
        model=model,
        messages=[ChatMessage(role="user", content="hi")],
        stream=stream,
        stream_options={"include_usage": True} if usage else None,
    )
