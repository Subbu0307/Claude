"""WhatsApp Cloud API: sending messages, verifying webhooks and parsing inbound payloads."""

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 4096  # WhatsApp's limit for a text message body


@dataclass(frozen=True)
class InboundMessage:
    message_id: str
    sender: str  # E.164 digits without '+', as WhatsApp sends it
    profile_name: str | None
    type: str
    text: str | None


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 header against the raw request body."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict[str, Any]) -> list[InboundMessage]:
    """Extract user messages from a webhook payload, ignoring delivery/read status updates."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                msg_type = m.get("type", "unknown")
                if msg_type == "text":
                    text = m.get("text", {}).get("body")
                elif msg_type == "interactive":
                    reply = m.get("interactive", {})
                    text = (reply.get("button_reply") or reply.get("list_reply") or {}).get("title")
                elif msg_type == "button":
                    text = m.get("button", {}).get("text")
                else:
                    text = None
                messages.append(
                    InboundMessage(
                        message_id=m["id"],
                        sender=m["from"],
                        profile_name=names.get(m["from"]),
                        type=msg_type,
                        text=text,
                    )
                )
    return messages


def split_text(text: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Split a long reply into WhatsApp-sized chunks, preferring paragraph then line breaks."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


class WhatsAppClient:
    def __init__(self, access_token: str, phone_number_id: str, api_version: str = "v21.0"):
        self._url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self._http = httpx.Client(headers={"Authorization": f"Bearer {access_token}"}, timeout=15)

    def send_text(self, to: str, text: str) -> None:
        """Send a text message. Failures are logged, not raised, so one bad send can't break a turn.

        Note: WhatsApp only delivers free-form text within 24h of the recipient's last message to
        you; reaching users outside that window requires an approved template message.
        """
        for chunk in split_text(text):
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            }
            try:
                resp = self._http.post(self._url, json=payload)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                detail = getattr(getattr(e, "response", None), "text", "")
                log.error("WhatsApp send to %s failed: %s %s", to, e, detail)
                return

    def mark_read(self, message_id: str) -> None:
        try:
            self._http.post(
                self._url, json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id}
            )
        except httpx.HTTPError:
            log.warning("Could not mark %s as read", message_id)


class ConsoleNotifier:
    """Stands in for WhatsApp when running locally: prints outbound messages to other users."""

    def send_text(self, to: str, text: str) -> None:
        print(f"\n[WhatsApp -> +{to}]\n{text}\n")
