"""WhatsApp Cloud API: webhook parsing and signature checks, sending text and images, downloading photos."""

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Inbound:
    message_id: str
    sender: str
    type: str  # text | image | other
    text: str = ""
    media_id: str = ""
    mime: str = ""


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def parse_webhook(payload: dict[str, Any]) -> list[Inbound]:
    out = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for m in change.get("value", {}).get("messages", []):
                t = m.get("type")
                if t == "text":
                    out.append(Inbound(m["id"], m["from"], "text", text=m["text"]["body"]))
                elif t == "image":
                    img = m.get("image", {})
                    out.append(Inbound(m["id"], m["from"], "image", text=img.get("caption", ""),
                                       media_id=img.get("id", ""), mime=img.get("mime_type", "image/jpeg")))
                elif t in ("button", "interactive"):
                    text = (m.get("button", {}).get("text")
                            or (m.get("interactive", {}).get("button_reply") or {}).get("title", ""))
                    out.append(Inbound(m["id"], m["from"], "text", text=text))
                else:
                    out.append(Inbound(m["id"], m["from"], "other"))
    return out


class WhatsAppClient:
    MAX_PHOTO_BYTES = 5 * 1024 * 1024

    def __init__(self, access_token: str, phone_number_id: str, api_version: str = "v21.0"):
        self.base = f"https://graph.facebook.com/{api_version}"
        self.phone_number_id = phone_number_id
        self.http = httpx.Client(headers={"Authorization": f"Bearer {access_token}"}, timeout=20)

    def send_text(self, to: str, text: str) -> None:
        self._post_message({"to": to, "type": "text", "text": {"body": text[:4096]}})

    def send_image(self, to: str, image: bytes, mime: str, caption: str = "") -> None:
        upload = self.http.post(
            f"{self.base}/{self.phone_number_id}/media",
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": ("footwear", image, mime)},
        )
        upload.raise_for_status()
        self._post_message({"to": to, "type": "image", "image": {"id": upload.json()["id"], "caption": caption}})

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        meta = self.http.get(f"{self.base}/{media_id}")
        meta.raise_for_status()
        info = meta.json()
        if int(info.get("file_size", 0)) > self.MAX_PHOTO_BYTES:
            raise ValueError("Photo too large")
        data = self.http.get(info["url"])  # this URL also needs the bearer token
        data.raise_for_status()
        return data.content, info.get("mime_type", "image/jpeg")

    def mark_read(self, message_id: str) -> None:
        try:
            self._post_message({"status": "read", "message_id": message_id})
        except httpx.HTTPError:
            log.warning("Could not mark %s read", message_id)

    def _post_message(self, body: dict) -> None:
        resp = self.http.post(f"{self.base}/{self.phone_number_id}/messages",
                              json={"messaging_product": "whatsapp", **body})
        resp.raise_for_status()
