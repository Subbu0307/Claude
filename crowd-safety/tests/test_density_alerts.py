import pytest

from crowdwatch import alerts as alerts_mod
from crowdwatch.alerts import Alert, AlertPolicy, Dispatcher, WhatsAppNotifier
from crowdwatch.config import Recipient
from crowdwatch.density import ZoneTracker


def tracker(settings, **kw):
    return ZoneTracker(settings.zones[0], smoothing=1.0, clear_seconds=30, **kw)


def kinds(events):
    return [(e.kind, e.level) for e in events]


def test_escalates_immediately_and_skips_levels(settings):
    t = tracker(settings)
    assert t.update(100, 0) == []  # 1/m²
    assert kinds(t.update(450, 2)) == [("level_up", "critical")]  # straight to critical at 4.5/m²
    assert t.state.level == "critical" and t.state.density == 4.5


def test_deescalates_only_after_sustained_drop(settings):
    t = tracker(settings)
    t.update(450, 0)
    # Hovering just under critical (within 10%) never downgrades: avoids flapping alerts.
    for s in range(2, 100, 2):
        assert t.update(380, s) == []
    # Clearly lower: downgrade only after clear_seconds.
    assert t.update(250, 100) == []
    assert t.update(250, 120) == []
    assert kinds(t.update(250, 131)) == [("level_down", "busy")]


def test_drop_timer_resets_if_density_bounces_back(settings):
    t = tracker(settings)
    t.update(450, 0)
    t.update(200, 10)
    t.update(395, 25)  # back near critical: timer resets
    assert t.update(200, 45) == []
    assert kinds(t.update(200, 76)) == [("level_down", "busy")]


def test_rising_fast_predicts_time_to_critical(settings):
    t = tracker(settings)
    events = []
    for i, s in enumerate(range(0, 61, 5)):  # 1.8 -> 3.0 /m² in a minute: 1.2/m² per minute
        events += t.update(180 + i * 10, s)
    rising = [e for e in events if e.kind == "rising_fast"]
    assert rising
    assert rising[-1].rise_per_minute == pytest.approx(1.2, rel=0.05)
    assert rising[-1].minutes_to_critical == pytest.approx(1.0 / 1.2, rel=0.1)


def test_count_multiplier_and_stale(settings):
    z = settings.zones[0]
    z.count_multiplier = 1.5
    t = tracker(settings)
    t.update(200, 0)
    assert t.state.count == 300 and t.state.density == 3.0
    t.mark_stale()
    assert t.state.stale is True


class Recorder:
    def __init__(self):
        self.alerts = []

    def __call__(self, alert):
        self.alerts.append(alert)


def test_policy_alerts_on_warning_up_not_busy(settings, clock):
    rec = Recorder()
    policy = AlertPolicy("Temple", rec, clock=clock)
    t = tracker(settings)
    for e in t.update(250, 0):  # busy: dashboard only
        policy.on_zone_event(e)
    assert rec.alerts == []
    for e in t.update(320, 2):
        policy.on_zone_event(e)
    assert rec.alerts[0].level == "warning" and "WARNING" in rec.alerts[0].title
    assert "Slow the inflow" in rec.alerts[0].body


def test_policy_repeats_critical_until_it_eases(settings, clock):
    rec = Recorder()
    policy = AlertPolicy("Temple", rec, repeat_critical_seconds=120, clock=clock)
    t = tracker(settings)
    for e in t.update(450, 0):
        policy.on_zone_event(e)
    assert "STOP ENTRY" in rec.alerts[-1].body
    clock.advance(60)
    policy.tick({"gate": t.state})
    assert len(rec.alerts) == 1
    clock.advance(61)
    policy.tick({"gate": t.state})
    assert rec.alerts[-1].title.startswith("🔴 STILL CRITICAL") and "4.5 people/m²" in rec.alerts[-1].body

    for e in t.update(150, 10) + t.update(150, 45):
        policy.on_zone_event(e)
    assert "Easing" in rec.alerts[-1].title
    assert rec.alerts[-1].level == "critical"  # reaches everyone who got the critical alert
    clock.advance(500)
    n = len(rec.alerts)
    policy.tick({"gate": t.state})
    assert len(rec.alerts) == n


def test_rising_alert_has_cooldown(settings, clock):
    rec = Recorder()
    policy = AlertPolicy("Temple", rec, rising_cooldown_seconds=300, clock=clock)
    t = tracker(settings)
    for i, s in enumerate(range(0, 101, 5)):  # +0.96/m² per minute from 2.0, for 100 s
        for e in t.update(200 + i * 8, s):
            policy.on_zone_event(e)
        clock.advance(5)
    rising = [a for a in rec.alerts if "building fast" in a.title]
    assert len(rising) == 1


def test_dispatcher_routes_by_level_and_retries(monkeypatch):
    monkeypatch.setattr(alerts_mod.time, "sleep", lambda s: None)
    sent = []

    class Flaky:
        calls = 0

        def send(self, r, a):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("network down")
            sent.append((r.name, a.level))

    recipients = [Recipient("Room", "console", min_level="busy"), Recipient("Police", "console", min_level="critical")]
    d = Dispatcher(recipients, {"console": Flaky()})
    d.submit(Alert("warning", "t", "b", "k1"))
    d.submit(Alert("critical", "t", "b", "k2"))
    d.close()
    assert sent == [("Room", "warning"), ("Room", "critical"), ("Police", "critical")]


def test_whatsapp_template_param_has_no_newlines():
    n = WhatsAppNotifier("tok", "123", template_name="crowd_alert")
    p = n.payload("9198", Alert("critical", "🔴 CRITICAL: Gate", "4.5/m²\nSTOP ENTRY", "k"))
    param = p["template"]["components"][0]["parameters"][0]["text"]
    assert "\n" not in param and "STOP ENTRY" in param
    assert WhatsAppNotifier("tok", "123").payload("9198", Alert("warning", "a", "b", "k"))["type"] == "text"
