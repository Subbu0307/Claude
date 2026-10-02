import json

from marketplace.agent import MarketplaceAgent
from marketplace.tools import TOOLS

from .conftest import FakeClaude, make_message


def tool_use(id_: str, name: str, input_: dict) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def test_buyer_places_order_and_seller_is_notified(store, outbox):
    store.ensure_user("111", "Sam")
    listing = store.create_listing("111", "Guitar", "Acoustic", "music", 12000, 1)

    fake = FakeClaude([
        make_message([tool_use("t1", "place_order", {"listing_id": listing["id"], "quantity": 1})], "tool_use"),
        make_message([text("Done! Order placed.")]),
    ])
    agent = MarketplaceAgent(store, notify=outbox, client=fake)

    reply = agent.handle_message("222", "Yes, buy it", profile_name="Priya")

    assert reply == "Done! Order placed."
    assert store.get_listing(listing["id"])["status"] == "sold_out"
    assert outbox.sent[0][0] == "111" and "Priya" in outbox.sent[0][1]

    # The tool result went back to Claude in a single user message, tied to the tool_use id.
    tool_turn = fake.requests[1]["messages"][-1]
    assert tool_turn["role"] == "user"
    assert tool_turn["content"][0]["tool_use_id"] == "t1"
    assert json.loads(tool_turn["content"][0]["content"])["seller_notified"] is True

    # Request shape: strict tools, effort, caching and refusal fallbacks.
    req = fake.requests[0]
    assert req["model"] == "claude-opus-5-5"
    assert req["tools"] is TOOLS and all(t["strict"] for t in TOOLS)
    assert req["fallbacks"] == "default"
    assert req["cache_control"] == {"type": "ephemeral"}


def test_tool_errors_are_reported_to_claude(store, outbox):
    fake = FakeClaude([
        make_message([tool_use("t1", "place_order", {"listing_id": 999, "quantity": 1})], "tool_use"),
        make_message([text("That listing doesn't exist.")]),
    ])
    agent = MarketplaceAgent(store, notify=outbox, client=fake)
    agent.handle_message("222", "buy 999")

    result = fake.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "does not exist" in result["content"]
    assert outbox.sent == []


def test_history_persists_across_messages_and_reset_clears_it(store, outbox):
    fake = FakeClaude([make_message([text("Hi! What are you selling?")]), make_message([text("Great.")])])
    agent = MarketplaceAgent(store, notify=outbox, client=fake)

    agent.handle_message("333", "I want to sell")
    agent.handle_message("333", "A bike")
    sent = fake.requests[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"] == [{"type": "text", "text": "Hi! What are you selling?"}]

    assert "fresh" in agent.handle_message("333", "reset")
    assert store.load_conversation("333")[0] == []


def test_refusal_is_not_stored(store, outbox):
    fake = FakeClaude([make_message([], "refusal")])
    agent = MarketplaceAgent(store, notify=outbox, client=fake)
    reply = agent.handle_message("444", "something disallowed")
    assert "can't help" in reply
    assert store.load_conversation("444")[0] == []


def test_actions_survive_an_interrupted_turn(store, outbox):
    """If the API fails after a tool ran, the record of that action is kept for the next turn."""
    import anthropic
    import httpx2

    store.ensure_user("111")
    listing = store.create_listing("111", "Mug", "", "home", 500, 3)

    class Failing(FakeClaude):
        def create(self, **kwargs):
            if not self.responses:
                raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x"))
            return super().create(**kwargs)

    fake = Failing([make_message([tool_use("t1", "place_order", {"listing_id": listing["id"], "quantity": 1})],
                                 "tool_use")])
    agent = MarketplaceAgent(store, notify=outbox, client=fake)
    assert "went wrong" in agent.handle_message("222", "buy the mug")

    stored, _ = store.load_conversation("222")
    assert stored[-1]["role"] == "user" and stored[-1]["content"][0]["type"] == "tool_result"

    fake.responses.append(make_message([text("Your order is in.")]))
    assert agent.handle_message("222", "did it work?") == "Your order is in."
    last = fake.requests[-1]["messages"][-1]
    assert last["content"][0]["type"] == "tool_result" and last["content"][-1]["text"] == "did it work?"


def test_tools_are_scoped_to_sender(store, outbox):
    store.ensure_user("111")
    listing = store.create_listing("111", "TV", "", "electronics", 30000, 1)
    fake = FakeClaude([
        make_message([tool_use("t1", "update_listing", {"listing_id": listing["id"], "price": 1})], "tool_use"),
        make_message([text("You can't edit that.")]),
    ])
    MarketplaceAgent(store, notify=outbox, client=fake).handle_message("999", "make TV cost 1")
    assert store.get_listing(listing["id"])["price_cents"] == 30000
