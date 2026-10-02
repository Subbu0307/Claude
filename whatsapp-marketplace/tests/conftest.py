import sys
from pathlib import Path

import pytest
from anthropic.types.beta import BetaMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from marketplace.store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "test.db"))


class Outbox:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def __call__(self, to: str, text: str) -> None:
        self.sent.append((to, text))


@pytest.fixture
def outbox():
    return Outbox()


def make_message(content: list[dict], stop_reason: str = "end_turn") -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
    )


class FakeClaude:
    """Mimics client.beta.messages.create, replaying scripted responses and recording requests."""

    def __init__(self, responses: list[BetaMessage]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.beta = self
        self.messages = self

    def create(self, **kwargs):
        # Snapshot: the agent keeps appending to the same list after the call.
        self.requests.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        return self.responses.pop(0)
