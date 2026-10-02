import numpy as np
from fastapi.testclient import TestClient

from crowdwatch.alerts import AlertPolicy
from crowdwatch.detector import _drop_cut_boxes, decode_yolox, foot_points, tile_windows
from crowdwatch.monitor import Monitor
from crowdwatch.server import create_app


class FakeDetector:
    def __init__(self):
        self.n = 0

    def detect(self, frame):
        # n people standing inside the zone, plus one outside it (x > 1000) that must not count.
        boxes = [[x % 900 + 10, 100, x % 900 + 30, 150 + (x % 700), 0.9] for x in range(self.n)]
        boxes.append([1100, 100, 1120, 150, 0.9])
        return np.asarray(boxes, dtype=np.float32)


def make_monitor(settings, clock):
    alerts = []
    det = FakeDetector()
    policy = AlertPolicy(settings.site_name, alerts.append, clock=clock)
    mon = Monitor(settings, det, policy, source_factory=lambda cam: None, clock=clock)
    return mon, det, alerts


FRAME = np.zeros((1080, 1280, 3), dtype=np.uint8)


def test_counts_people_in_zone_and_alerts(settings, clock):
    mon, det, alerts = make_monitor(settings, clock)
    cam = settings.cameras[0]
    det.n = 150
    mon.process_frame(cam, FRAME)
    zone = mon.status()["cameras"][0]["zones"][0]
    assert zone["count"] == 150 and zone["level"] == "normal" and zone["stale"] is False
    det.n = 420
    clock.advance(2)
    mon.process_frame(cam, FRAME)
    assert alerts[-1].level == "critical"
    assert mon.snapshots["cam1"][:2] == b"\xff\xd8"  # JPEG


def test_camera_offline_and_recovery(settings, clock):
    mon, det, alerts = make_monitor(settings, clock)
    cam = settings.cameras[0]
    mon.process_frame(cam, FRAME)
    clock.advance(25)
    mon.check_health()
    assert "OFFLINE" in alerts[-1].title and "Gate" in alerts[-1].body
    status = mon.status()["cameras"][0]
    assert status["online"] is False and status["zones"][0]["stale"] is True
    clock.advance(5)
    mon.check_health()
    assert len(alerts) == 1  # not repeated
    mon.process_frame(cam, FRAME)
    assert "back online" in alerts[-1].title


def test_camera_that_never_connects_is_reported(settings, clock):
    mon, _, alerts = make_monitor(settings, clock)
    clock.advance(21)
    mon.check_health()
    assert "OFFLINE" in alerts[-1].title


def test_dashboard_requires_password(settings, clock):
    mon, _, _ = make_monitor(settings, clock)
    client = TestClient(create_app(mon, password="s3cret"))
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/status", auth=("staff", "wrong")).status_code == 401
    ok = client.get("/api/status", auth=("staff", "s3cret"))
    assert ok.status_code == 200 and ok.json()["site"] == "Test Temple"
    assert "CrowdWatch" in client.get("/", auth=("staff", "s3cret")).text
    assert client.get("/api/cameras/cam1/snapshot.jpg", auth=("staff", "s3cret")).status_code == 404
    assert client.get("/health").status_code == 200


def test_decode_yolox_grid():
    raw = np.zeros((8400, 85), dtype=np.float32)
    raw[1, :4] = [0.5, 0.5, 0, 0]  # second cell of the stride-8 grid, unit size
    out = decode_yolox(raw, (640, 640))
    assert out[1, :4].tolist() == [12.0, 4.0, 8.0, 8.0]
    assert out.shape == (8400, 85)


def test_tiles_cover_frame_and_cut_boxes_dropped():
    wins = list(tile_windows(1280, 720, 2, 2))
    assert len(wins) == 4
    assert min(w[0] for w in wins) == 0 and max(w[2] for w in wins) == 1280
    assert max(w[3] for w in wins) == 720
    boxes = np.array([[700, 100, 760, 300, 0.9], [100, 100, 150, 300, 0.9]], dtype=np.float32)
    kept = _drop_cut_boxes(boxes, (0, 0, 762, 400), 1280, 720)  # first box touches the right cut edge
    assert kept.tolist() == boxes[1:].tolist()


def test_foot_points():
    assert foot_points(np.array([[10, 20, 30, 80, 0.9]])).tolist() == [[20, 80]]


def test_drill_keeps_each_cameras_people_separate(settings, clock):
    """Two cameras whose zones overlap in pixel space must not count each other's simulated people."""
    import copy

    from crowdwatch.cli import DrillDetector
    from crowdwatch.config import parse_settings

    from .conftest import BASE

    raw = copy.deepcopy(BASE)
    cam2 = copy.deepcopy(raw["cameras"][0])
    cam2.update(id="cam2", name="Hall cam")
    cam2["zones"][0].update(id="hall", name="Hall")
    raw["cameras"].append(cam2)
    s = parse_settings(raw)
    det = DrillDetector(s.zones, ramp_seconds=60)
    det.start -= 75  # first zone mid-way through its 5/m² peak
    mon = Monitor(s, det, AlertPolicy(s.site_name, lambda a: None, clock=clock),
                  source_factory=det.source_for, clock=clock)
    for cam in s.cameras:
        mon.process_frame(cam, det.source_for(cam).read())
    densities = {z["zone_id"]: z["density"] for c in mon.status()["cameras"] for z in c["zones"]}
    assert densities["gate"] == 5.0
    assert densities["hall"] < 5.0  # staggered start, and not inflated by cam1's people
