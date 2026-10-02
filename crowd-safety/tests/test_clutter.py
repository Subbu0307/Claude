import copy

import cv2
import numpy as np
from fastapi.testclient import TestClient

from crowdwatch.alerts import AlertPolicy
from crowdwatch.clutter import ClutterTracker
from crowdwatch.config import ClutterSettings, Thresholds, Zone, parse_settings
from crowdwatch.monitor import Monitor
from crowdwatch.server import create_app

from .conftest import BASE

NO_PEOPLE = np.zeros((0, 5), dtype=np.float32)
RNG = np.random.default_rng(1)
FLOOR = cv2.GaussianBlur(RNG.integers(60, 140, (480, 640, 3), dtype=np.uint8), (5, 5), 0)  # paving texture
SHOES = [(100, 300), (160, 320), (300, 350), (420, 290), (520, 400), (250, 420)]


def noisy(img):
    return np.clip(img.astype(int) + RNG.normal(0, 4, img.shape), 0, 255).astype(np.uint8)


def with_shoes(img, spots=SHOES):
    out = img.copy()
    for x, y in spots:
        cv2.ellipse(out, (x, y), (22, 9), 15, 0, 360, (25, 25, 30), -1)
    return out


def make(**kw):
    settings = ClutterSettings(alert_seconds=10, **kw)
    zone = Zone("steps", "Exit steps", "cam1", [(0, 250), (640, 250), (640, 479), (0, 479)], 20, Thresholds(),
                keep_clear=settings)
    t = ClutterTracker(zone, settings)
    t.set_reference(FLOOR)
    return t


def feed(t, frames, start=0.0, step=2.0, boxes=NO_PEOPLE, people=0, density=0.0):
    events = []
    for i, f in enumerate(frames):
        events += t.update(f, boxes, people, density, start + i * step)
    return events


def test_footwear_left_on_steps_triggers_alert_after_delay():
    t = make()
    events = feed(t, [noisy(with_shoes(FLOOR)) for _ in range(4)])
    assert events == []  # not yet: must persist alert_seconds
    events = feed(t, [noisy(with_shoes(FLOOR)) for _ in range(10)], start=8)
    assert [e.kind for e in events] == ["clutter"]
    assert t.state.objects == 6 and t.state.alerting


def test_lighting_change_and_noise_are_not_clutter():
    t = make()
    feed(t, [noisy((FLOOR * 0.7).astype(np.uint8)) for _ in range(15)])
    feed(t, [noisy(np.clip(FLOOR * 1.2, 0, 255).astype(np.uint8)) for _ in range(15)], start=40)
    assert t.state.objects == 0


def test_passing_feet_are_not_clutter():
    t = make()
    frames = [noisy(with_shoes(FLOOR) if i % 4 == 0 else FLOOR) for i in range(30)]
    assert feed(t, frames) == []
    assert t.state.objects == 0


def test_people_standing_on_the_floor_are_masked_out():
    t = make()
    person = with_shoes(FLOOR, [(320, 400)])  # something under a detected person's box
    boxes = np.array([[290, 250, 350, 410, 0.9]], dtype=np.float32)
    feed(t, [noisy(person) for _ in range(15)], boxes=boxes, people=1, density=0.05)
    assert t.state.objects == 0


def test_crowded_zone_is_not_judged():
    t = make()
    feed(t, [noisy(with_shoes(FLOOR)) for _ in range(15)], people=60, density=3.0)
    assert t.state.floor_visible is False and t.state.objects == 0


def test_cleared_after_footwear_removed_and_reminders_repeat():
    t = make(repeat_seconds=60)
    feed(t, [noisy(with_shoes(FLOOR)) for _ in range(10)])
    events = feed(t, [noisy(with_shoes(FLOOR)) for _ in range(40)], start=20)
    assert [e.kind for e in events].count("clutter") >= 1  # reminder while still uncleared
    events = feed(t, [noisy(FLOOR) for _ in range(30)], start=200)
    assert events[-1].kind == "cleared" and not t.state.alerting


def test_reference_learned_automatically_when_zone_empty_and_reset_on_size_change():
    settings = ClutterSettings()
    zone = Zone("s", "S", "c", [(0, 0), (100, 0), (100, 100), (0, 100)], 5, Thresholds(), keep_clear=settings)
    t = ClutterTracker(zone, settings)
    t.update(FLOOR, NO_PEOPLE, 3, 0.1, 0)
    assert not t.state.has_reference  # people present: wait
    t.update(FLOOR, NO_PEOPLE, 0, 0.0, 2)
    assert t.state.has_reference

    t2 = ClutterTracker(zone, settings)
    t2.load_reference(np.zeros((10, 10), dtype=np.float32))  # saved at another resolution
    t2.update(FLOOR, NO_PEOPLE, 3, 0.1, 0)
    assert not t2.state.has_reference


class NoPeople:
    def detect(self, frame):
        return NO_PEOPLE


def test_monitor_alerts_mark_cleared_and_persists_reference(tmp_path, clock):
    raw = copy.deepcopy(BASE)
    raw["state_dir"] = str(tmp_path)
    zone = raw["cameras"][0]["zones"][0]
    zone["polygon"] = [[0, 250], [640, 250], [640, 479], [0, 479]]
    zone["keep_clear"] = {"alert_seconds": 10}
    s = parse_settings(raw)
    alerts = []
    mon = Monitor(s, NoPeople(), AlertPolicy(s.site_name, alerts.append, clock=clock),
                  source_factory=lambda c: None, clock=clock)
    cam = s.cameras[0]
    mon.process_frame(cam, noisy(FLOOR))  # empty zone: learns the clean floor
    assert (tmp_path / "clean_floor_gate.npy").exists()
    for _ in range(12):
        clock.advance(2)
        mon.process_frame(cam, noisy(with_shoes(FLOOR)))
    assert "Footwear piling up" in alerts[-1].title

    client = TestClient(create_app(mon, password=""))
    assert client.post("/api/zones/gate/mark-cleared").status_code == 200
    assert "Clear again" in alerts[-1].title
    assert client.get("/api/status").json()["cameras"][0]["zones"][0]["clutter"]["objects"] == 0
    assert client.post("/api/zones/nope/mark-cleared").status_code == 404

    # After a restart the saved reference is reused: footwear already there is still detected.
    mon2 = Monitor(s, NoPeople(), AlertPolicy(s.site_name, alerts.append, clock=clock),
                   source_factory=lambda c: None, clock=clock)
    assert mon2.clutter["gate"].state.has_reference


def test_plain_uniform_floor_does_not_turn_noise_into_objects():
    plain = np.full((480, 640, 3), 120, dtype=np.uint8)  # polished stone: almost no texture
    zone = Zone("s", "S", "c", [(0, 250), (640, 250), (640, 479), (0, 479)], 20, Thresholds(),
                keep_clear=ClutterSettings(alert_seconds=10))
    t = ClutterTracker(zone, zone.keep_clear)
    t.set_reference(plain)
    feed(t, [noisy(plain) for _ in range(15)])
    assert t.state.objects == 0
    feed(t, [noisy(with_shoes(plain)) for _ in range(15)], start=40)
    assert t.state.objects == 6


def test_one_big_heap_alerts_by_floor_coverage():
    t = make()
    heap = FLOOR.copy()
    cv2.rectangle(heap, (200, 300), (330, 380), (25, 25, 30), -1)  # one heap of footwear, ~7% of the zone
    events = feed(t, [noisy(heap) for _ in range(15)])
    assert t.state.objects == 1 and [e.kind for e in events] == ["clutter"]
