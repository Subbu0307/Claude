"""CrowdWatch command line.

  crowdwatch check  site.yaml [--frame IMG]   validate config, print zone areas, draw zones on a frame
  crowdwatch run    site.yaml                  monitor cameras, serve dashboard, send alerts
  crowdwatch drill  site.yaml                  rehearse alerts with simulated crowds (no cameras needed)
"""

import argparse
import logging
import math
import os
import random
import sys
import time

import cv2
import numpy as np

from .alerts import AlertPolicy, ConsoleNotifier, Dispatcher, WebhookNotifier, WhatsAppNotifier
from .config import ConfigError, Settings, Zone, load_settings
from .geometry import points_in_polygon
from .monitor import Monitor

log = logging.getLogger("crowdwatch")


def build_notifiers() -> dict:
    notifiers = {"console": ConsoleNotifier(), "webhook": WebhookNotifier()}
    token, phone_id = os.environ.get("WHATSAPP_ACCESS_TOKEN"), os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
    if token and phone_id:
        notifiers["whatsapp"] = WhatsAppNotifier(
            token, phone_id,
            template_name=os.environ.get("WHATSAPP_ALERT_TEMPLATE", ""),
            template_language=os.environ.get("WHATSAPP_ALERT_TEMPLATE_LANG", "en"),
        )
    return notifiers


def build_monitor(settings: Settings, detector, source_factory=None) -> tuple[Monitor, Dispatcher]:
    notifiers = build_notifiers()
    for r in settings.recipients:
        if r.channel not in notifiers:
            log.error("Recipient %s uses %s, which is not configured (set WHATSAPP_* env vars)", r.name, r.channel)
    dispatcher = Dispatcher(settings.recipients, notifiers)
    policy = AlertPolicy(settings.site_name, dispatcher.submit, settings.repeat_critical_seconds)
    return Monitor(settings, detector, policy, source_factory=source_factory), dispatcher


def serve(monitor: Monitor, host: str, port: int) -> None:
    import uvicorn

    from .server import create_app

    if host not in ("127.0.0.1", "localhost") and not os.environ.get("CROWDWATCH_DASHBOARD_PASSWORD"):
        sys.exit("Refusing to expose the dashboard on the network without CROWDWATCH_DASHBOARD_PASSWORD.")
    print(f"Dashboard: http://{host}:{port}/")
    uvicorn.run(create_app(monitor), host=host, port=port, log_level="warning")


# ---- check ---------------------------------------------------------------------------------

def cmd_check(args) -> None:
    settings = load_settings(args.config)
    print(f"Site: {settings.site_name}")
    for cam in settings.cameras:
        print(f"\nCamera {cam.id} ({cam.name}) <- {cam.source}")
        for z in cam.zones:
            t = z.thresholds
            print(f"  zone {z.id:<14} {z.name:<28} area {z.area_m2:7.1f} m²  "
                  f"critical at {t.critical}/m² ≈ {math.floor(t.critical * z.area_m2)} people")
            if z.area_m2 < 4:
                print("    ! very small area: a couple of people will trigger alerts; check calibration")
    for r in settings.recipients:
        print(f"Recipient {r.name}: {r.channel} {r.address} (from {r.min_level})")
    if args.frame:
        frame = cv2.imread(args.frame)
        if frame is None:
            sys.exit(f"Cannot read {args.frame}")
        cam = next((c for c in settings.cameras if c.id == args.camera), settings.cameras[0])
        detector = None
        if os.path.exists(settings.model_path):
            from .detector import YoloxDetector, foot_points

            detector = YoloxDetector(settings.model_path, settings.detection_confidence, tiles=settings.tiles)
            boxes = detector.detect(frame)
            feet = foot_points(boxes)
            for z in cam.zones:
                n = int(points_in_polygon(feet, z.polygon).sum())
                print(f"  {z.name}: {n} people detected -> {n * z.count_multiplier / z.area_m2:.2f}/m²")
        else:
            boxes = np.zeros((0, 5))
            print(f"(model {settings.model_path} not found: drawing zones only)")
        for z in cam.zones:
            pts = np.asarray(z.polygon, dtype=np.int32)
            cv2.polylines(frame, [pts], True, (0, 200, 255), 2)
            cv2.putText(frame, z.name, tuple(pts[0]), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 200, 255), 2)
        for x1, y1, x2, y2, _ in boxes.astype(int):
            cv2.rectangle(frame, (x1, y1), (x2, y2), (255, 255, 255), 1)
            cv2.circle(frame, ((x1 + x2) // 2, y2), 3, (0, 0, 255), -1)
        cv2.imwrite(args.out, frame)
        print(f"Wrote {args.out}: check every zone covers the floor area you meant.")


# ---- run -----------------------------------------------------------------------------------

def cmd_run(args) -> None:
    from .detector import YoloxDetector

    settings = load_settings(args.config)
    if not os.path.exists(settings.model_path):
        sys.exit(f"Model not found at {settings.model_path}. See README: download yolox_s.onnx.")
    detector = YoloxDetector(settings.model_path, settings.detection_confidence, tiles=settings.tiles)
    monitor, dispatcher = build_monitor(settings, detector)
    monitor.start()
    try:
        serve(monitor, args.host, args.port)
    finally:
        monitor.stop()
        dispatcher.close()


# ---- drill ---------------------------------------------------------------------------------

class DrillSource:
    def __init__(self, camera_id: str, width=1280, height=720):
        self.camera_id = camera_id
        self.frame = np.full((height, width, 3), 40, dtype=np.uint8)
        cv2.putText(self.frame, "DRILL - SIMULATED CROWD", (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 200, 255), 2)

    def read(self):
        return self.frame

    def close(self):
        pass


class DrillDetector:
    """Fakes person detections so each zone's density follows a ramp: builds past critical, holds,
    then eases. Zones start at staggered times so alerts arrive one after another."""

    def __init__(self, zones: list[Zone], ramp_seconds: float, peak: float = 5.0):
        self.zones = zones
        self.frame_camera: dict[int, str] = {}  # which camera each drill frame belongs to
        self.ramp = ramp_seconds
        self.peak = peak
        self.start = time.monotonic()
        self.rng = random.Random(7)

    def density_at(self, index: int, t: float) -> float:
        t -= index * self.ramp / 3
        if t < 0:
            return 1.0
        if t < self.ramp:
            return 1.0 + (self.peak - 1.0) * t / self.ramp
        if t < self.ramp * 1.5:
            return self.peak
        return max(1.0, self.peak - (self.peak - 1.0) * (t - self.ramp * 1.5) / self.ramp)

    def source_for(self, camera) -> "DrillSource":
        source = DrillSource(camera.id)
        self.frame_camera[id(source.frame)] = camera.id
        return source

    def detect(self, frame):
        t = time.monotonic() - self.start
        camera_id = self.frame_camera.get(id(frame))
        boxes = []
        for i, z in enumerate(self.zones):
            if z.camera_id != camera_id:
                continue
            n = int(self.density_at(i, t) * z.area_m2 / z.count_multiplier)
            pts = np.asarray(z.polygon)
            (x0, y0), (x1, y1) = pts.min(axis=0), pts.max(axis=0)
            placed = 0
            while placed < n:
                cand = np.column_stack([[self.rng.uniform(x0, x1) for _ in range(n)],
                                        [self.rng.uniform(y0, y1) for _ in range(n)]])
                for x, y in cand[points_in_polygon(cand, z.polygon)][: n - placed]:
                    boxes.append([x - 6, y - 30, x + 6, y, 0.9])
                    placed += 1
        return np.asarray(boxes, dtype=np.float32).reshape(-1, 5)


def cmd_drill(args) -> None:
    settings = load_settings(args.config)
    settings.clear_seconds = min(settings.clear_seconds, args.ramp / 4)
    print(f"DRILL: simulated crowds ramp past critical over {args.ramp:.0f}s per zone. "
          "Alerts go to the configured recipients, so tell them it's a drill first.")
    detector = DrillDetector(settings.zones, args.ramp)
    monitor, dispatcher = build_monitor(settings, detector, source_factory=detector.source_for)
    monitor.start()
    try:
        serve(monitor, args.host, args.port)
    finally:
        monitor.stop()
        dispatcher.close()


def main(argv=None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="crowdwatch", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="validate config and preview zones")
    p.add_argument("config")
    p.add_argument("--frame", help="an image from the camera to draw zones and detections on")
    p.add_argument("--camera", help="which camera the frame comes from (default: first)")
    p.add_argument("--out", default="zones_preview.jpg")
    p.set_defaults(func=cmd_check)

    for name, func, helptext in (("run", cmd_run, "monitor cameras"), ("drill", cmd_drill, "simulated drill")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("config")
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8080)
        if name == "drill":
            p.add_argument("--ramp", type=float, default=120, help="seconds for a zone to go from 1 to 5 people/m²")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ConfigError as e:
        sys.exit(f"Config error: {e}")


if __name__ == "__main__":
    main()
