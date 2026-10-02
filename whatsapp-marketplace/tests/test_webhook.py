import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from marketplace.app import create_app
from marketplace.config import Settings
from marketplace.whatsapp import parse_webhook, split_text


class FakeWhatsApp:
    def __init__(self):
        self.sent = []
        self.read = []

    def send_text(self, to, text):
        self.sent.append((to, text))

    def mark_read(self, message_id):
        self.read.append(message_id)


class EchoAgent:
    def handle_message(self, phone, text, profile_name=None):
        return f"echo:{text}"


def payload(msg_id="wamid.1", body="hello", msg_type="text"):
    message = {"from": "15551234567", "id": msg_id, "timestamp": "0", "type": msg_type}
    if msg_type == "text":
        message["text"] = {"body": body}
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "contacts": [{"wa_id": "15551234567", "profile": {"name": "Asha"}}],
            "messages": [message],
        }}]}],
    }


def make_client(store, secret=""):
    settings = Settings(whatsapp_verify_token="tok", whatsapp_app_secret=secret)
    wa = FakeWhatsApp()
    app = create_app(settings=settings, store=store, whatsapp=wa, agent=EchoAgent())
    return TestClient(app), wa


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verification_handshake(store):
    client, _ = make_client(store)
    ok = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    bad = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"})
    assert bad.status_code == 403


def test_message_flow_and_dedup(store):
    client, wa = make_client(store)
    for _ in range(2):  # WhatsApp may deliver the same message twice
        assert client.post("/webhook", json=payload()).status_code == 200
    assert wa.sent == [("15551234567", "echo:hello")]
    assert wa.read == ["wamid.1"]


def test_non_text_message_gets_hint(store):
    client, wa = make_client(store)
    client.post("/webhook", json=payload(msg_type="image"))
    assert "text messages" in wa.sent[0][1]


def test_signature_enforced_when_secret_set(store):
    client, wa = make_client(store, secret="s3cret")
    body = json.dumps(payload()).encode()
    assert client.post("/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=bad"}).status_code == 401
    ok = client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign("s3cret", body)})
    assert ok.status_code == 200 and wa.sent


def test_parse_ignores_status_updates():
    status_only = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x", "status": "read"}]}}]}]}
    assert parse_webhook(status_only) == []
    [msg] = parse_webhook(payload(body="hi"))
    assert (msg.sender, msg.profile_name, msg.text) == ("15551234567", "Asha", "hi")


def test_split_text():
    long = "\n\n".join(["x" * 3000, "y" * 3000])
    assert split_text(long) == ["x" * 3000, "y" * 3000]
    assert split_text("z" * 5000) == ["z" * 4096, "z" * 904]
