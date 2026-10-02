"""Decides which events become alerts, words them, and delivers them to staff."""

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import httpx

from .config import LEVELS, Recipient
from .density import ZoneEvent

log = logging.getLogger(__name__)

ICONS = {"normal": "🟢", "busy": "🟡", "warning": "🟠", "critical": "🔴"}

ACTIONS = {
    "warning": "Slow the inflow: hold people at the outer barricade and open alternate routes.",
    "critical": "STOP ENTRY to this area now. Open exits, deploy stewards, keep people moving out. "
    "Make calm announcements; do not use alarming language.",
}


@dataclass
class Alert:
    level: str  # used to route to recipients with min_level <= level
    title: str
    body: str
    key: str  # dedupe key
    created_at: float = field(default_factory=time.time)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}"


class Notifier(Protocol):
    def send(self, recipient: Recipient, alert: Alert) -> None: ...


class ConsoleNotifier:
    def send(self, recipient: Recipient, alert: Alert) -> None:
        print(f"\n[ALERT -> {recipient.name}]\n{alert.text}\n", flush=True)


class WhatsAppNotifier:
    """WhatsApp Cloud API. Staff who haven't messaged the number in 24h can only receive an approved
    *template*, so production setups should set template_name (a template with one {{1}} body
    parameter, e.g. "Crowd alert: {{1}}")."""

    def __init__(self, access_token: str, phone_number_id: str, template_name: str = "",
                 template_language: str = "en", api_version: str = "v21.0"):
        self.url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
        self.template_name = template_name
        self.template_language = template_language
        self.http = httpx.Client(headers={"Authorization": f"Bearer {access_token}"}, timeout=10)

    def payload(self, to: str, alert: Alert) -> dict:
        if self.template_name:
            # Template parameters may not contain newlines.
            param = " | ".join(line for line in alert.text.splitlines() if line.strip())[:1000]
            return {
                "messaging_product": "whatsapp", "to": to, "type": "template",
                "template": {
                    "name": self.template_name,
                    "language": {"code": self.template_language},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": param}]}],
                },
            }
        return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": alert.text[:4096]}}

    def send(self, recipient: Recipient, alert: Alert) -> None:
        resp = self.http.post(self.url, json=self.payload(recipient.address, alert))
        resp.raise_for_status()


class WebhookNotifier:
    """POSTs JSON to any URL: an SMS gateway, a PA system controller, a police control room."""

    def __init__(self):
        self.http = httpx.Client(timeout=10)

    def send(self, recipient: Recipient, alert: Alert) -> None:
        resp = self.http.post(recipient.address, json={
            "level": alert.level, "title": alert.title, "body": alert.body, "key": alert.key,
            "created_at": alert.created_at,
        })
        resp.raise_for_status()


class Dispatcher:
    """Delivers alerts on a background thread so a slow network never delays analysis.
    Each send is retried; one recipient failing doesn't stop the others."""

    def __init__(self, recipients: list[Recipient], notifiers: dict[str, Notifier], retries: int = 3):
        self.recipients = recipients
        self.notifiers = notifiers
        self.retries = retries
        self._queue: queue.Queue[Alert | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="alert-dispatch", daemon=True)
        self._thread.start()

    def submit(self, alert: Alert) -> None:
        self._queue.put(alert)

    def close(self, timeout: float = 5) -> None:
        self._queue.put(None)
        self._thread.join(timeout)

    def _run(self) -> None:
        while (alert := self._queue.get()) is not None:
            for r in self.recipients:
                if LEVELS.index(alert.level) < LEVELS.index(r.min_level):
                    continue
                notifier = self.notifiers.get(r.channel)
                if notifier is None:
                    log.error("No notifier configured for channel %s (recipient %s)", r.channel, r.name)
                    continue
                for attempt in range(1, self.retries + 1):
                    try:
                        notifier.send(r, alert)
                        break
                    except Exception as e:  # noqa: BLE001 - any delivery failure is retried and logged
                        log.warning("Alert to %s failed (attempt %d): %s", r.name, attempt, e)
                        time.sleep(min(2 ** attempt, 10))
                else:
                    log.error("Giving up on alert %s to %s", alert.key, r.name)


class AlertPolicy:
    """Turns zone and camera events into alerts, with repeats and cooldowns so staff are told
    what matters once, clearly, and reminded while a critical condition persists."""

    def __init__(self, site_name: str, emit: Callable[[Alert], None], repeat_critical_seconds: float = 120,
                 rising_cooldown_seconds: float = 300, clock: Callable[[], float] = time.monotonic):
        self.site = site_name
        self.emit = emit
        self.repeat_critical_seconds = repeat_critical_seconds
        self.rising_cooldown_seconds = rising_cooldown_seconds
        self.clock = clock
        self._last_sent: dict[str, float] = {}
        self._critical_zones: dict[str, ZoneEvent] = {}

    def on_zone_event(self, e: ZoneEvent) -> None:
        z = e.zone
        where = f"{z.name} ({self.site})"
        stats = f"{e.density:.1f} people/m² · ~{e.count} people in {z.area_m2:.0f} m²"
        if e.kind == "level_up" and LEVELS.index(e.level) >= LEVELS.index("warning"):
            title = f"{ICONS[e.level]} {e.level.upper()}: {where}"
            self._send(Alert(e.level, title, f"{stats}\n{ACTIONS[e.level]}", f"{z.id}:{e.level}"))
            if e.level == "critical":
                self._critical_zones[z.id] = e
        elif e.kind == "level_down":
            self._critical_zones.pop(z.id, None)
            if LEVELS.index(e.previous_level) >= LEVELS.index("warning"):
                title = f"{ICONS[e.level]} Easing: {where} now {e.level}"
                # Route to everyone who got the original alert.
                self._send(Alert(e.previous_level, title, stats, f"{z.id}:down:{e.level}"))
        elif e.kind == "rising_fast":
            key = f"{z.id}:rising"
            last = self._last_sent.get(key)
            if last is not None and self.clock() - last < self.rising_cooldown_seconds:
                return
            eta = f" Critical in ~{e.minutes_to_critical:.0f} min at this rate." if e.minutes_to_critical else ""
            title = f"📈 Crowd building fast: {where}"
            body = f"{stats}, rising {e.rise_per_minute:.1f}/m² per minute.{eta}\n{ACTIONS['warning']}"
            self._send(Alert("warning", title, body, key))

    def on_clutter(self, e) -> None:
        z = e.zone
        if e.kind == "clutter":
            self._send(Alert(
                "warning", f"👟 Footwear piling up: {z.name} ({self.site})",
                f"{e.objects} item(s) or heap(s) have been lying on the floor here for over a minute. Send a volunteer "
                "to clear them: in a rush, people trip over footwear on steps and in exits. "
                "Press 'Mark cleared' on the dashboard when done.",
                f"{z.id}:clutter",
            ))
        else:
            self._send(Alert("warning", f"✅ Clear again: {z.name}", "Floor is clear.", f"{z.id}:clutter:clear"))

    def on_camera(self, camera_name: str, online: bool, zone_names: list[str]) -> None:
        if online:
            alert = Alert("warning", f"✅ Camera back online: {camera_name}",
                          "Monitoring resumed for: " + ", ".join(zone_names), f"cam:{camera_name}:up")
        else:
            alert = Alert("warning", f"⚠️ Camera OFFLINE: {camera_name} ({self.site})",
                          "No monitoring for: " + ", ".join(zone_names) + ". Post a steward to watch these "
                          "areas until the feed is restored.", f"cam:{camera_name}:down")
        self._send(alert)

    def tick(self, states: dict | None = None) -> None:
        """Called periodically: re-alerts zones that remain critical, with their latest reading."""
        now = self.clock()
        for zone_id, e in list(self._critical_zones.items()):
            key = f"{zone_id}:critical"
            if now - self._last_sent.get(key, now) < self.repeat_critical_seconds:
                continue
            body = ACTIONS["critical"]
            state = (states or {}).get(zone_id)
            if state is not None:
                body = (f"{state.density:.1f} people/m² · ~{state.count} people"
                        + (" (camera feed lost)" if state.stale else "") + f"\n{body}")
            self._send(Alert("critical", f"🔴 STILL CRITICAL: {e.zone.name} ({self.site})", body, key))

    def _send(self, alert: Alert) -> None:
        self._last_sent[alert.key] = self.clock()
        log.warning("ALERT %s | %s", alert.title, alert.body.replace("\n", " | "))
        self.emit(alert)
