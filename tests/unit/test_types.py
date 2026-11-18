import pytest
from pydantic import SecretStr, ValidationError

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


def make_request(**extra: object) -> ChatRequest:
    return ChatRequest.model_validate(
        {"model": "fast-chat", "messages": [{"role": "user", "content": "Hi"}], **extra}
    )


def test_unknown_request_fields_are_kept_for_pass_through() -> None:
    req = make_request(logit_bias={"50256": -100}, my_custom_option=True)
    assert req.extra_params() == {"logit_bias": {"50256": -100}, "my_custom_option": True}
    again = ChatRequest.model_validate_json(req.model_dump_json())
    assert again.extra_params() == req.extra_params()


def test_unknown_message_fields_are_kept() -> None:
    message = ChatMessage.model_validate({"role": "user", "content": "Hi", "cache_control": "x"})
    assert message.model_extra == {"cache_control": "x"}


def test_a_request_needs_at_least_one_message() -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"model": "m", "messages": []})


@pytest.mark.parametrize("bad", [{"temperature": 3}, {"top_p": 1.5}, {"max_tokens": 0}])
def test_out_of_range_numbers_are_rejected(bad: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        make_request(**bad)


def test_stream_usage_flag() -> None:
    assert make_request().wants_stream_usage is False
    assert make_request(stream_options={"include_usage": True}).wants_stream_usage is True


def test_usage_total_is_worked_out_when_missing() -> None:
    assert Usage(prompt_tokens=3, completion_tokens=4).total_tokens == 7


def test_usage_total_from_the_provider_is_kept() -> None:
    assert Usage(prompt_tokens=3, completion_tokens=4, total_tokens=9).total_tokens == 9


def test_response_text_helper() -> None:
    resp = ChatResponse(
        model="m", choices=[Choice(message=ChatMessage(role="assistant", content="Hello"))]
    )
    assert resp.text == "Hello"
    assert resp.object == "chat.completion"
    assert resp.id.startswith("chatcmpl-")


def test_response_text_is_empty_when_there_are_no_choices() -> None:
    assert ChatResponse(model="m", choices=[]).text == ""


def test_chunk_helpers() -> None:
    chunk = ChatChunk(model="m", choices=[ChunkChoice(delta=Delta(content="abc"))])
    assert chunk.text == "abc"
    assert chunk.finish_reason is None
    done = ChatChunk(model="m", choices=[ChunkChoice(finish_reason="stop")])
    assert done.text == ""
    assert done.finish_reason == "stop"
    assert ChatChunk(model="m").finish_reason is None
    assert ChatChunk(model="m").text == ""


def test_targets_cannot_be_changed_and_can_be_used_as_dict_keys() -> None:
    target = Target(provider_id="groq", model="m", key_ids=("k1",))
    with pytest.raises(ValidationError):
        target.model = "other"  # type: ignore[misc]
    assert {target: 1}[Target(provider_id="groq", model="m", key_ids=("k1",))] == 1


def test_a_secret_never_shows_in_text_or_logs() -> None:
    key = ResolvedKey(id="k1", secret=SecretStr("sk-very-secret"))
    assert "sk-very-secret" not in repr(key)
    assert "sk-very-secret" not in str(key)
    assert "sk-very-secret" not in key.model_dump_json()
    assert key.secret.get_secret_value() == "sk-very-secret"
