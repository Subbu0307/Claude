import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from finder.app import create_app
from finder.whatsapp import parse_webhook


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    def send_text(self, to, text):
        self.sent.append((to, "text", text))

    def send_image(self, to, image, mime, caption=""):
        self.sent.append((to, "image", image))

    def download_media(self, media_id):
        return b"photo-" + media_id.encode(), "image/jpeg"

    def mark_read(self, message_id):
        pass


def message(mid, kind="text", body="SAVE E1", caption=""):
    m = {"from": "919811111111", "id": mid, "timestamp": "0", "type": kind}
    if kind == "text":
        m["text"] = {"body": body}
    elif kind == "image":
        m["image"] = {"id": "media1", "mime_type": "image/jpeg", "caption": caption}
    return {"entry": [{"changes": [{"value": {"messages": [m]}}]}]}


def make(config, store, secret=""):
    config.app_secret = secret
    config.verify_token = "tok"
    config.stats_password = "pw"
    wa = FakeWhatsApp()
    return TestClient(create_app(config, store=store, whatsapp=wa)), wa


def test_full_visit_over_webhook(config, store):
    client, wa = make(config, store)
    client.post("/webhook", json=message("m1"))
    client.post("/webhook", json=message("m1"))  # WhatsApp redelivery: ignored
    client.post("/webhook", json=message("m2", kind="image"))
    client.post("/webhook", json=message("m3", body="I'm done with darshan"))
    kinds = [k for _, k, _ in wa.sent]
    assert kinds == ["text", "text", "text", "image"]
    assert "Saved" in wa.sent[0][2] and "E1" in wa.sent[2][2]
    assert wa.sent[3][2] == b"photo-media1"

    stats = client.get("/api/spots", auth=("staff", "pw")).json()["spots"]
    assert next(s for s in stats if s["code"] == "E1")["saved"] == 1
    assert client.get("/api/spots").status_code == 401


def test_photo_with_code_caption(config, store):
    client, wa = make(config, store)
    client.post("/webhook", json=message("m1", kind="image", caption="E2"))
    assert "Saved: your footwear is at *E2" in wa.sent[-1][2]
    assert store.get("919811111111")["photo"] == b"photo-media1"


def test_signature_and_verification(config, store):
    client, wa = make(config, store, secret="s3cret")
    body = json.dumps(message("m1")).encode()
    assert client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=0"}).status_code == 401
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sig}).status_code == 200
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "7"})
    assert ok.text == "7"


def test_parse_ignores_status_updates():
    assert parse_webhook({"entry": [{"changes": [{"value": {"statuses": [{"id": "x"}]}}]}]}) == []
