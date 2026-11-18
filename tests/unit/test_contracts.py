"""The plugin and routing policy contracts, shown with tiny working examples."""

from collections.abc import Sequence

from prism.core.context import RequestContext
from prism.core.errors import ErrorKind, PrismError
from prism.core.policies.base import RoutingPolicy
from prism.core.types import ChatMessage, ChatRequest, ChatResponse, Choice, Target
from prism.plugins.base import Continue, Decision, Plugin, Reject, ShortCircuit


def make_ctx(text: str = "Hi") -> RequestContext:
    request = ChatRequest(model="fast", messages=[ChatMessage(role="user", content=text)])
    return RequestContext(request=request, route_name="fast")


class BlockWord:
    """Rejects any request that contains a banned word."""

    name = "block-word"

    async def before_request(self, ctx: RequestContext) -> Decision:
        for message in ctx.request.messages:
            if isinstance(message.content, str) and "forbidden" in message.content:
                return Reject(PrismError(ErrorKind.CONTENT_BLOCKED, "blocked by policy"))
        return Continue()

    async def after_response(self, ctx: RequestContext, resp: ChatResponse) -> None:
        ctx.metadata["seen_response"] = resp.id


class AlwaysCached:
    name = "always-cached"

    def __init__(self, answer: ChatResponse) -> None:
        self.answer = answer

    async def before_request(self, ctx: RequestContext) -> Decision:
        return ShortCircuit(self.answer)

    async def after_response(self, ctx: RequestContext, resp: ChatResponse) -> None:
        return None


class ReverseOrder:
    name = "reverse"

    def order(self, targets: Sequence[Target], ctx: RequestContext) -> list[Target]:
        return list(reversed(targets))


async def test_a_plugin_can_continue_or_reject() -> None:
    plugin = BlockWord()
    assert isinstance(plugin, Plugin)
    assert isinstance(await plugin.before_request(make_ctx("hello")), Continue)
    decision = await plugin.before_request(make_ctx("a forbidden word"))
    assert isinstance(decision, Reject)
    assert decision.error.kind is ErrorKind.CONTENT_BLOCKED


async def test_a_plugin_can_answer_without_calling_the_provider() -> None:
    answer = ChatResponse(
        model="m", choices=[Choice(message=ChatMessage(role="assistant", content="cached"))]
    )
    decision = await AlwaysCached(answer).before_request(make_ctx())
    assert isinstance(decision, ShortCircuit)
    assert decision.response.text == "cached"


async def test_after_response_can_leave_notes_in_the_context() -> None:
    ctx = make_ctx()
    answer = ChatResponse(model="m", choices=[])
    await BlockWord().after_response(ctx, answer)
    assert ctx.metadata["seen_response"] == answer.id


def test_a_routing_policy_returns_targets_in_the_order_to_try() -> None:
    policy = ReverseOrder()
    assert isinstance(policy, RoutingPolicy)
    first, second = Target(provider_id="a", model="m"), Target(provider_id="b", model="m")
    assert policy.order([first, second], make_ctx()) == [second, first]


def test_things_that_are_not_plugins_or_policies_are_rejected() -> None:
    assert not isinstance(object(), Plugin)
    assert not isinstance(object(), RoutingPolicy)


def test_every_request_gets_its_own_id_and_a_running_clock() -> None:
    first, second = make_ctx(), make_ctx()
    assert first.request_id != second.request_id
    assert first.elapsed_s() >= 0
    assert first.attempt == 0
