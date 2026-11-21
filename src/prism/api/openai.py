"""OpenAI-compatible routes: /v1/chat/completions and /v1/models."""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from prism.core.context import RequestContext
from prism.core.errors import ErrorKind, PrismError
from prism.core.gateway import Gateway
from prism.core.governance.keys import Authenticator, KeyRecord
from prism.core.types import ChatChunk, ChatRequest, Target

log = logging.getLogger(__name__)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_bearer = HTTPBearer(auto_error=False, description="Prism virtual key (pk-prism-...)")


def request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing:
        return str(existing)
    incoming = request.headers.get("x-request-id", "")
    value = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
    request.state.request_id = value
    return value


async def require_virtual_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    """Check the bearer token and keep the key record on the request.

    Fails closed: with no authenticator and auth not explicitly disabled,
    every request is refused.
    """
    request.state.key = None
    if request.app.state.auth_disabled:
        return
    authenticator: Authenticator | None = request.app.state.authenticator
    if authenticator is None:
        raise PrismError(ErrorKind.UNAUTHENTICATED, "Invalid or missing API key.")
    token = credentials.credentials if credentials else None
    request.state.key = await authenticator.authenticate(token)


def current_key(request: Request) -> KeyRecord | None:
    return getattr(request.state, "key", None)


def get_gateway(request: Request) -> Gateway:
    gateway: Gateway | None = request.app.state.gateway
    if gateway is None:
        raise PrismError(
            ErrorKind.ROUTE_NOT_FOUND, "No routes are configured. Start Prism with --config."
        )
    return gateway


router = APIRouter(prefix="/v1", tags=["OpenAI"], dependencies=[Depends(require_virtual_key)])


def _headers(rid: str, target: Target | None) -> dict[str, str]:
    headers = {"x-prism-request-id": rid, "x-prism-cache": "miss" if target else "hit"}
    if target is not None:
        headers["x-prism-provider"] = target.provider_id
        headers["x-prism-model"] = target.model
    return headers


@router.post("/chat/completions", summary="Create chat completion")
async def chat_completions(
    body: ChatRequest,
    request: Request,
    gateway: Annotated[Gateway, Depends(get_gateway)],
) -> Any:
    rid = request_id(request)
    key = current_key(request)
    if key is not None:
        Authenticator.authorize(key, body.model)
    ctx = RequestContext(
        request=body,
        route_name=body.model,
        request_id=rid,
        key_id=key.id if key else None,
        team_id=key.team_id if key else None,
    )

    if not body.stream:
        result = await gateway.chat(body, ctx)
        return JSONResponse(
            result.response.model_dump(mode="json", exclude_none=True),
            headers=_headers(rid, result.target),
        )

    result = await gateway.stream(body, ctx)
    headers = {
        **_headers(rid, result.target),
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        _sse(result.chunks, rid), media_type="text/event-stream", headers=headers
    )


async def _sse(chunks: AsyncIterator[ChatChunk], rid: str) -> AsyncIterator[str]:
    try:
        async for chunk in chunks:
            yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
    except PrismError as error:
        yield f"data: {json.dumps(error.to_openai_error())}\n\n"
        return
    except Exception:
        log.exception("Stream %s failed", rid)
        internal = PrismError(ErrorKind.INTERNAL_ERROR, "An internal error occurred.")
        yield f"data: {json.dumps(internal.to_openai_error())}\n\n"
        return
    yield "data: [DONE]\n\n"


@router.get("/models", summary="List models")
async def list_models(request: Request) -> dict[str, Any]:
    gateway: Gateway | None = request.app.state.gateway
    key = current_key(request)
    names = [
        name
        for name in (gateway.route_names() if gateway else [])
        if key is None or key.allows(name)
    ]
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": "prism"} for name in names
        ],
    }
