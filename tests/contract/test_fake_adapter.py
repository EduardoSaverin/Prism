"""The fake provider has to pass the same contract as every real adapter."""

import pytest

from prism.providers.fake import FakeAdapter, FakeProviderError, FakeScript
from tests.contract.suite import AdapterContractSuite, AdapterHarness


class FakeHarness:
    def __init__(self) -> None:
        self.script = FakeScript()
        self.adapter = FakeAdapter(self.script)

    def reply_ok(self, text: str, *, prompt_tokens: int, completion_tokens: int) -> None:
        self.script.reply = text
        self.script.prompt_tokens = prompt_tokens
        self.script.completion_tokens = completion_tokens

    def reply_stream(
        self, pieces: list[str], *, prompt_tokens: int, completion_tokens: int
    ) -> None:
        self.script.stream_pieces = pieces
        self.script.reply = "".join(pieces)
        self.script.prompt_tokens = prompt_tokens
        self.script.completion_tokens = completion_tokens

    def fail_with_status(self, status: int) -> None:
        self.script.error = FakeProviderError(status)

    def fail_timeout(self) -> None:
        self.script.error = FakeProviderError(None, timeout=True)

    def fail_content_blocked(self) -> None:
        self.script.error = FakeProviderError(400, content_blocked=True)

    def drop_stream_after(self, pieces: list[str], after: int) -> None:
        self.script.stream_pieces = pieces
        self.script.break_stream_after = after


class TestFakeAdapterContract(AdapterContractSuite):
    @pytest.fixture
    def harness(self) -> AdapterHarness:
        return FakeHarness()
