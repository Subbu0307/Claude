"""Webhook server.  FINDER_CONFIG=finder.yaml uvicorn finder.app:app --host 0.0.0.0 --port 8000"""

import functools
import json
import logging
import os
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .bot import FinderBot, Reply
from .config import FinderConfig, load_config
from .store import Store
from .whatsapp import Inbound, WhatsAppClient, parse_webhook, verify_signature

log = logging.getLogger(__name__)
security = HTTPBasic(auto_error=False)


def create_app(config: FinderConfig, store: Store | None = None, whatsapp=None) -> FastAPI:
    store = store or Store(config.db_path)
    whatsapp = whatsapp or WhatsAppClient(config.access_token, config.phone_number_id, config.api_version)
    bot = FinderBot(config, store)
    if not config.app_secret:
        log.warning("WHATSAPP_APP_SECRET is not set: webhook signatures will NOT be verified. Dev use only.")
    app = FastAPI(title="Footwear Finder")

    @app.get("/webhook", response_class=PlainTextResponse)
    def verify(mode: str = Query("", alias="hub.mode"), token: str = Query("", alias="hub.verify_token"),
               challenge: str = Query("", alias="hub.challenge")) -> str:
        if mode == "subscribe" and config.verify_token and token == config.verify_token:
            return challenge
        raise HTTPException(403, "Verification failed")

    def send(to: str, replies: list[Reply]) -> None:
        for r in replies:
            if r.image:
                whatsapp.send_image(to, r.image, r.image_mime, r.text)
            else:
                whatsapp.send_text(to, r.text)

    def process(msg: Inbound) -> None:
        if not store.mark_processed(msg.message_id):
            return
        store.purge(config.retention_hours * 3600)
        whatsapp.mark_read(msg.message_id)
        try:
            if msg.type == "image":
                photo, mime = whatsapp.download_media(msg.media_id)
                replies = bot.handle_photo(msg.sender, photo, mime)
                if bot.mentions_spot(msg.text):  # caption such as "E3": save spot, confirm both
                    replies = bot.handle_text(msg.sender, msg.text)
            elif msg.type == "text":
                replies = bot.handle_text(msg.sender, msg.text)
            else:
                replies = [Reply("Please send text or a photo. Send *HELP* to see how this works.")]
        except Exception:
            log.exception("Failed handling %s", msg.message_id)
            replies = [Reply("Sorry, something went wrong. Please try again, or ask a volunteer.")]
        try:
            send(msg.sender, replies)
        except Exception:
            log.exception("Failed replying to %s", msg.message_id)

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> dict:
        body = await request.body()
        if config.app_secret and not verify_signature(config.app_secret, body,
                                                      request.headers.get("X-Hub-Signature-256")):
            raise HTTPException(401, "Bad signature")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid JSON")
        for msg in parse_webhook(payload):
            background.add_task(process, msg)
        return {"status": "received"}

    def stats_auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not config.stats_password or creds is None or not secrets.compare_digest(
                creds.password.encode(), config.stats_password.encode()):
            raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Basic"})

    @app.get("/api/spots", dependencies=[Depends(stats_auth)])
    def spots() -> dict:
        """How many people currently have footwear saved at each spot: shows staff where footwear
        builds up, so they can place boards, volunteers and cleaners there."""
        counts = store.counts_by_spot()
        return {"spots": [{"code": s.code, "name": s.name, "saved": counts.get(s.code, 0)}
                          for s in config.spots.values()]}

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


@functools.cache
def _default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    return create_app(load_config(os.environ.get("FINDER_CONFIG", "finder.yaml")))


def __getattr__(name: str):
    if name == "app":
        return _default_app()
    raise AttributeError(name)
