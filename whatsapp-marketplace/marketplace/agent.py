"""The Claude agent that runs each user's marketplace conversation."""

import logging
import threading
import time
from collections import defaultdict
from typing import Any

import anthropic

from .store import Store
from .tools import TOOLS, Notify, ToolExecutor

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are the assistant for a peer-to-peer marketplace that people use entirely through WhatsApp. \
Anyone can buy and sell here: they chat with you, and you use your tools to search listings, \
publish listings, place orders and manage them. Every tool acts as the person you are chatting with.

How to help:
- Buyers: understand what they want, search, and present a short list of matches with listing id, \
title, price and seller location. When they pick one, restate item, quantity and total and get a \
clear yes before calling place_order.
- Sellers: collect title, a useful description (condition, pickup/delivery), category, price and \
quantity — ask only for what is missing — then confirm and call create_listing. Help them manage \
listings and respond to incoming orders (accept, mark shipped, or cancel).
- New users: if get_my_profile shows no location, ask for their city/area once it becomes relevant \
(e.g. before their first listing or order) and save it.
- Only state facts that come from tool results. Never invent listings, prices, stock or order states. \
If a tool returns an error, explain it plainly and suggest the next step.
- Payment and handover are arranged directly between buyer and seller once an order is accepted; \
the marketplace does not take payments. Remind both sides to meet in safe public places and never \
pay in advance to someone they have not verified.

Style: this is WhatsApp. Keep replies short and scannable, in the user's language. Use WhatsApp \
formatting only (*bold*, _italic_, simple "- " or "1." lists); no Markdown headings, tables or links \
in brackets. Refer to listings as "#<id>" so users can reply with the number.
"""

RESET_COMMANDS = {"reset", "/reset", "restart", "/restart", "start over"}
# Start a fresh conversation after this much inactivity (matches WhatsApp's 24h service window).
CONVERSATION_TTL_SECONDS = 24 * 60 * 60
# Hard cap on stored turns; beyond it we start fresh rather than editing history mid-conversation.
MAX_STORED_MESSAGES = 120
MAX_AGENT_STEPS = 12

FALLBACK_REPLY = "Sorry, something went wrong on my side. Please try again in a moment."


class MarketplaceAgent:
    def __init__(
        self,
        store: Store,
        notify: Notify,
        client: anthropic.Anthropic | None = None,
        model: str = "claude-opus-5-5",
        effort: str = "low",
        currency: str = "USD",
    ):
        self.store = store
        self.notify = notify
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.currency = currency
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    def handle_message(self, phone: str, text: str, profile_name: str | None = None) -> str:
        """Process one inbound WhatsApp text and return the reply to send back."""
        # One turn at a time per user, so rapid-fire messages don't interleave their histories.
        with self._locks[phone]:
            self.store.ensure_user(phone, profile_name)
            if text.strip().lower() in RESET_COMMANDS:
                self.store.clear_conversation(phone)
                return "Starting fresh 👋 What would you like to buy or sell?"
            return self._run_turn(phone, text)

    def _load_history(self, phone: str) -> list[dict[str, Any]]:
        messages, updated_at = self.store.load_conversation(phone)
        if updated_at is not None and time.time() - updated_at > CONVERSATION_TTL_SECONDS:
            return []
        if len(messages) > MAX_STORED_MESSAGES:
            return []
        return messages

    def _run_turn(self, phone: str, text: str) -> str:
        history = self._load_history(phone)
        if history and history[-1]["role"] == "user":
            # A previous turn was cut short after its tool results; continue in that same user turn.
            previous = history[-1]["content"]
            if isinstance(previous, str):
                previous = [{"type": "text", "text": previous}]
            messages = history[:-1] + [{"role": "user", "content": previous + [{"type": "text", "text": text}]}]
        else:
            messages = history + [{"role": "user", "content": text}]
        executor = ToolExecutor(self.store, phone, self.notify, self.currency)

        for _ in range(MAX_AGENT_STEPS):
            try:
                response = self._call_claude(messages)
            except anthropic.APIError:
                log.exception("Claude API call failed for %s", phone)
                self._save_partial(phone, history, messages)
                return FALLBACK_REPLY

            if response.stop_reason == "refusal":
                self._save_partial(phone, history, messages)
                return "Sorry, I can't help with that here. Is there something you'd like to buy or sell?"

            # Keep every block (thinking, tool_use, fallback...) exactly as returned: the API expects
            # them echoed back unchanged on the next request.
            messages.append({"role": "assistant", "content": [b.to_dict() for b in response.content]})

            if response.stop_reason == "tool_use":
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    output, is_error = executor.run(block.name, dict(block.input))
                    log.info("tool %s(%s) -> error=%s", block.name, block.input, is_error)
                    result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": output}
                    if is_error:
                        result["is_error"] = True
                    results.append(result)
                # All results for one assistant turn go back in a single user message.
                messages.append({"role": "user", "content": results})
                continue

            if response.stop_reason == "pause_turn":
                continue

            self.store.save_conversation(phone, messages)
            reply = "\n\n".join(b.text for b in response.content if b.type == "text").strip()
            return reply or "👍"

        log.warning("Agent hit MAX_AGENT_STEPS for %s", phone)
        self._save_partial(phone, history, messages)
        return FALLBACK_REPLY

    def _save_partial(self, phone: str, history: list[dict[str, Any]], messages: list[dict[str, Any]]) -> None:
        """Persist an unfinished turn. Tool calls that already ran (an order placed, a listing created)
        stay in the record; a turn that produced nothing is dropped so it isn't retried forever."""
        if messages[-1]["role"] == "assistant":
            messages = messages[:-1]  # an unanswered tool_use would be rejected on the next request
        self.store.save_conversation(phone, messages if len(messages) > len(history) + 1 else history)

    def _call_claude(self, messages: list[dict[str, Any]]):
        return self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
            output_config={"effort": self.effort},
            # Cache the stable prefix (tools + system + earlier turns) across a user's messages.
            cache_control={"type": "ephemeral"},
            # If a safety classifier declines, let the API retry on a suitable fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
