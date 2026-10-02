"""FastAPI server exposing the WhatsApp Cloud API webhook.

Run with:  uvicorn marketplace.app:app --host 0.0.0.0 --port 8000
"""

import functools
import json
import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from .agent import MarketplaceAgent
from .config import Settings
from .store import Store
from .whatsapp import InboundMessage, WhatsAppClient, parse_webhook, verify_signature

log = logging.getLogger(__name__)

UNSUPPORTED_REPLY = "I can only read text messages for now — please type what you'd like to buy or sell."


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    whatsapp: WhatsAppClient | None = None,
    agent: MarketplaceAgent | None = None,
) -> FastAPI:
    settings = settings or Settings()
    store = store or Store(settings.db_path)
    whatsapp = whatsapp or WhatsAppClient(
        settings.whatsapp_access_token, settings.whatsapp_phone_number_id, settings.whatsapp_api_version
    )
    agent = agent or MarketplaceAgent(
        store, notify=whatsapp.send_text, model=settings.model, effort=settings.effort, currency=settings.currency
    )
    if not settings.whatsapp_app_secret:
        log.warning("WHATSAPP_APP_SECRET is not set: webhook signatures will NOT be verified. Dev use only.")

    app = FastAPI(title="WhatsApp Marketplace Agent")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/webhook", response_class=PlainTextResponse)
    def verify_webhook(
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ) -> str:
        """Meta calls this once when you register the webhook URL."""
        if mode == "subscribe" and settings.whatsapp_verify_token and token == settings.whatsapp_verify_token:
            return challenge
        raise HTTPException(status_code=403, detail="Verification failed")

    def process(msg: InboundMessage) -> None:
        if not store.mark_processed(msg.message_id):
            return  # WhatsApp redelivered a message we already handled
        whatsapp.mark_read(msg.message_id)
        if not msg.text:
            whatsapp.send_text(msg.sender, UNSUPPORTED_REPLY)
            return
        try:
            reply = agent.handle_message(msg.sender, msg.text, msg.profile_name)
        except Exception:
            log.exception("Failed handling message %s", msg.message_id)
            reply = "Sorry, something went wrong on my side. Please try again in a moment."
        whatsapp.send_text(msg.sender, reply)

    @app.post("/webhook")
    async def receive_webhook(request: Request, background: BackgroundTasks) -> dict[str, str]:
        body = await request.body()
        if settings.whatsapp_app_secret and not verify_signature(
            settings.whatsapp_app_secret, body, request.headers.get("X-Hub-Signature-256")
        ):
            raise HTTPException(status_code=401, detail="Bad signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        # Acknowledge immediately; Meta retries webhooks that take too long to answer.
        for msg in parse_webhook(payload):
            background.add_task(process, msg)
        return {"status": "received"}

    return app


@functools.cache
def _default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app()


def __getattr__(name: str):
    # Built lazily so importing this module (e.g. in tests) doesn't need credentials.
    if name == "app":
        return _default_app()
    raise AttributeError(name)
