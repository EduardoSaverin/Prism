import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from prism.config import ConfigError, load_config, parse_config
from prism.core.governance.keys import (
    Authenticator,
    InMemoryKeyCache,
    KeyRecord,
    generate_key,
    hash_key,
)
from prism.main import Governance, create_app
from prism.providers.fake import FakeProviderError
from prism.providers.registry import build_gateway
from tests.scripted import RetryAfterError, ScriptedAdapter, build, config_for, route

BODY = {"model": "chat", "messages": [{"role": "user", "content": "hi"}]}


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://test",
    )


PEPPER = "p" * 32


class DictLookup:
    def __init__(self) -> None:
        self.records: dict[str, KeyRecord] = {}

    def add(self, record: KeyRecord) -> str:
        secret = generate_key()
        self.records[hash_key(secret, PEPPER)] = record
        return secret

    async def find_by_hash(self, key_hash: str) -> KeyRecord | None:
        return self.records.get(key_hash)


def app_with(
    *adapters: ScriptedAdapter,
    lookup: DictLookup | None = None,
    routes: tuple[str, ...] = ("chat",),
    **route_extra: Any,
) -> Any:
    """An app over scripted adapters. Without a lookup, authentication is off."""
    names = [a.name for a in adapters]
    h = build(
        config_for(names, [route(r, *names, **route_extra) for r in routes]),
        {a.name: a for a in adapters},
    )
    if lookup is None:
        return create_app(h.gateway.config, gateway=h.gateway, auth_disabled=True)
    governance = Governance(Authenticator(lookup, InMemoryKeyCache(), PEPPER))
    return create_app(h.gateway.config, gateway=h.gateway, governance=governance)


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def sse_events(text: str) -> list[str]:
    return [line.removeprefix("data: ") for line in text.split("\n\n") if line.startswith("data: ")]


async def test_chat_completion_returns_the_answer_and_prism_headers() -> None:
    async with client_for(app_with(ScriptedAdapter("a", reply="hello"))) as client:
        response = await client.post(
            "/v1/chat/completions", json=BODY, headers={"x-request-id": "req-123"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "chat"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello"}
    assert body["usage"]["total_tokens"] == 15
    assert response.headers["x-prism-request-id"] == "req-123"
    assert response.headers["x-prism-provider"] == "a"
    assert response.headers["x-prism-model"] == "a-model"
    assert response.headers["x-prism-cache"] == "miss"


async def test_the_cost_is_not_sent_to_the_client() -> None:
    config = config_for(["a"], [route("chat", "a")])
    config["providers"][0]["prices"] = {"a-model": {"input": 1, "output": 1}}
    h = build(config, {"a": ScriptedAdapter("a")})
    app = create_app(h.gateway.config, gateway=h.gateway, auth_disabled=True)
    async with client_for(app) as client:
        response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200
    assert not any("cost" in name for name in response.headers)
    assert "cost" not in response.text


async def test_an_unsafe_request_id_is_replaced() -> None:
    async with client_for(app_with(ScriptedAdapter("a"))) as client:
        response = await client.post(
            "/v1/chat/completions", json=BODY, headers={"x-request-id": "bad id\nx"}
        )
    assert response.headers["x-prism-request-id"] != "bad id\nx"
    assert len(response.headers["x-prism-request-id"]) == 32


async def test_streaming_sends_sse_chunks_and_done() -> None:
    adapter = ScriptedAdapter("a", pieces=["Hel", "lo"])
    async with client_for(app_with(adapter)) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={**BODY, "stream": True, "stream_options": {"include_usage": True}},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-prism-provider"] == "a"
    events = sse_events(response.text)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "Hello"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["prompt_tokens"] == 10


async def test_a_stream_that_fails_midway_ends_with_an_error_event() -> None:
    adapter = ScriptedAdapter("a", pieces=["one", "two"], break_stream_after=1)
    async with client_for(app_with(adapter)) as client:
        response = await client.post("/v1/chat/completions", json={**BODY, "stream": True})
    events = sse_events(response.text)
    assert "[DONE]" not in events
    error = json.loads(events[-1])["error"]
    assert error["code"] == "upstream_unavailable"


async def test_a_stream_that_cannot_start_is_a_normal_error_response() -> None:
    adapter = ScriptedAdapter("a", errors=[FakeProviderError(400, "bad request")])
    async with client_for(app_with(adapter)) as client:
        response = await client.post("/v1/chat/completions", json={**BODY, "stream": True})
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_rate_limits_return_429_with_retry_after() -> None:
    adapter = ScriptedAdapter("a", errors=[RetryAfterError(429, 42)])
    async with client_for(app_with(adapter)) as client:
        response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "42"
    assert response.json() == {
        "error": {"message": "HTTP 429", "type": "rate_limit_error", "code": "rate_limited"}
    }
    assert "x-prism-request-id" in response.headers


async def test_an_unknown_model_is_a_404() -> None:
    async with client_for(app_with(ScriptedAdapter("a"))) as client:
        response = await client.post("/v1/chat/completions", json={**BODY, "model": "gpt-9"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


async def test_an_invalid_body_is_a_400_in_openai_format() -> None:
    async with client_for(app_with(ScriptedAdapter("a"))) as client:
        response = await client.post("/v1/chat/completions", json={"model": "chat"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert "messages" in error["message"]


async def test_models_lists_the_routes() -> None:
    async with client_for(app_with(ScriptedAdapter("a"))) as client:
        body = (await client.get("/v1/models")).json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["chat"]


async def test_without_a_config_there_are_no_models_and_chat_is_404() -> None:
    async with client_for(create_app(auth_disabled=True)) as client:
        assert (await client.get("/v1/models")).json()["data"] == []
        response = await client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 404


async def test_a_virtual_key_is_required() -> None:
    lookup = DictLookup()
    secret = lookup.add(KeyRecord(id="k1", team_id="t1"))
    async with client_for(app_with(ScriptedAdapter("a"), lookup=lookup)) as client:
        missing = await client.post("/v1/chat/completions", json=BODY)
        wrong = await client.get("/v1/models", headers=bearer("pk-prism-not-a-real-key"))
        not_ours = await client.get("/v1/models", headers=bearer("sk-something-else"))
        right = await client.post("/v1/chat/completions", json=BODY, headers=bearer(secret))
        health = await client.get("/healthz")
    assert missing.status_code == wrong.status_code == not_ours.status_code == 401
    assert missing.json()["error"]["code"] == "invalid_api_key"
    assert right.status_code == 200
    assert health.status_code == 200


async def test_without_governance_every_v1_request_is_refused() -> None:
    async with client_for(create_app()) as client:
        models = await client.get("/v1/models")
        health = await client.get("/healthz")
    assert models.status_code == 401
    assert health.status_code == 200


async def test_a_revoked_or_expired_key_is_refused() -> None:
    lookup = DictLookup()
    past = datetime.now(UTC) - timedelta(minutes=1)
    revoked = lookup.add(KeyRecord(id="k1", team_id="t1", revoked_at=past))
    expired = lookup.add(KeyRecord(id="k2", team_id="t1", expires_at=past))
    async with client_for(app_with(ScriptedAdapter("a"), lookup=lookup)) as client:
        r1 = await client.post("/v1/chat/completions", json=BODY, headers=bearer(revoked))
        r2 = await client.post("/v1/chat/completions", json=BODY, headers=bearer(expired))
    assert r1.status_code == r2.status_code == 401
    assert "revoked" in r1.json()["error"]["message"]
    assert "expired" in r2.json()["error"]["message"]


async def test_a_key_can_only_use_its_allowed_routes() -> None:
    lookup = DictLookup()
    secret = lookup.add(KeyRecord(id="k1", team_id="t1", allowed_routes=("chat",)))
    app = app_with(ScriptedAdapter("a"), lookup=lookup, routes=("chat", "other"))
    async with client_for(app) as client:
        allowed = await client.post("/v1/chat/completions", json=BODY, headers=bearer(secret))
        denied = await client.post(
            "/v1/chat/completions", json={**BODY, "model": "other"}, headers=bearer(secret)
        )
        models = (await client.get("/v1/models", headers=bearer(secret))).json()
    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"
    assert [m["id"] for m in models["data"]] == ["chat"]


async def test_a_key_with_no_route_list_sees_every_route() -> None:
    lookup = DictLookup()
    secret = lookup.add(KeyRecord(id="k1", team_id="t1"))
    app = app_with(ScriptedAdapter("a"), lookup=lookup, routes=("chat", "other"))
    async with client_for(app) as client:
        models = (await client.get("/v1/models", headers=bearer(secret))).json()
    assert [m["id"] for m in models["data"]] == ["chat", "other"]


async def test_the_openapi_spec_documents_the_v1_routes_and_auth() -> None:
    async with client_for(app_with(ScriptedAdapter("a"))) as client:
        spec = (await client.get("/openapi.json")).json()
    chat = spec["paths"]["/v1/chat/completions"]["post"]
    assert chat["security"]
    assert "ChatRequest" in json.dumps(chat["requestBody"])


async def test_the_example_config_builds_a_real_gateway(example_config_path: Path) -> None:
    gateway = build_gateway(load_config(example_config_path), {"GROQ_KEY_1": "x"})
    assert gateway.route_names() == ["fast-chat"]
    assert {a.name for a in gateway.adapters.values()} == {"groq", "gemini", "local-ollama"}
    await gateway.aclose()


def test_a_policy_from_a_later_phase_is_a_config_error() -> None:
    config = parse_config(config_for(["a"], [route("chat", "a", policy="cost")]))
    with pytest.raises(ConfigError, match="not available yet"):
        build_gateway(config, {})


def test_a_retired_provider_is_a_config_error() -> None:
    config = parse_config(
        {
            "providers": [{"id": "gh", "kind": "github_models", "keys": [{"env": "GH"}]}],
            "routes": [{"name": "chat", "targets": [{"provider": "gh", "model": "m"}]}],
        }
    )
    with pytest.raises(ConfigError, match="retired"):
        build_gateway(config, {})


def _upstream(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer groq-secret"
    assert payload["model"] == "llama-test"
    if not payload.get("stream"):
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "llama-test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "real hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )
    base = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "llama-test"}
    lines = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {**base, "choices": [{"index": 0, "delta": {"content": "real "}}]},
        {**base, "choices": [{"index": 0, "delta": {"content": "stream"}}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**base, "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(line)}\n\n" for line in lines) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


async def test_end_to_end_through_a_real_adapter() -> None:
    from prism.providers.openai_compat import OpenAICompatAdapter
    from prism.providers.registry import PRESETS

    config = parse_config(
        {
            "providers": [{"id": "groq", "kind": "groq", "keys": [{"env": "GROQ"}]}],
            "routes": [{"name": "chat", "targets": [{"provider": "groq", "model": "llama-test"}]}],
        }
    )
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(_upstream))
    gateway = build_gateway(
        config,
        {"GROQ": "groq-secret"},
        adapter_factory=lambda p: OpenAICompatAdapter(p.id, PRESETS[p.kind], client=upstream),
    )
    async with client_for(create_app(config, gateway=gateway, auth_disabled=True)) as client:
        plain = await client.post("/v1/chat/completions", json=BODY)
        streamed = await client.post("/v1/chat/completions", json={**BODY, "stream": True})
    await upstream.aclose()

    assert plain.status_code == 200
    assert plain.json()["choices"][0]["message"]["content"] == "real hello"
    assert plain.json()["model"] == "chat"
    events = sse_events(streamed.text)
    chunks = [json.loads(e) for e in events[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "real stream"
    assert all("usage" not in c for c in chunks)
    assert events[-1] == "[DONE]"


async def test_the_app_starts_and_stops_governance_services() -> None:
    events: list[str] = []

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        async def start(self) -> None:
            events.append(f"start {self.name}")

        async def aclose(self) -> None:
            events.append(f"stop {self.name}")

    governance = Governance(
        Authenticator(DictLookup(), InMemoryKeyCache(), PEPPER),
        [Recorder("watcher"), Recorder("db")],
    )
    app = create_app(governance=governance)
    async with app.router.lifespan_context(app):
        assert events == ["start watcher", "start db"]
    assert events[2:] == ["stop db", "stop watcher"]
