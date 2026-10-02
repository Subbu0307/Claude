"""Reads every camera, counts people per zone, and feeds the alert policy."""

import logging
import threading
import time
from typing import Callable, Protocol

import cv2
import numpy as np

from .alerts import AlertPolicy
from .config import Camera, Settings
from .density import ZoneTracker
from .detector import Detector, foot_points
from .geometry import points_in_polygon

log = logging.getLogger(__name__)

LEVEL_COLORS_BGR = {"normal": (80, 175, 76), "busy": (0, 200, 255), "warning": (0, 140, 255),
                    "critical": (40, 40, 220)}


class FrameSource(Protocol):
    def read(self) -> np.ndarray | None:
        """Latest frame, or None if none is available right now."""

    def close(self) -> None: ...


class LiveSource:
    """Continuously drains an RTSP/HTTP/webcam stream on its own thread and keeps only the newest
    frame. Without this, frames queue up inside the decoder and analysis falls minutes behind."""

    def __init__(self, url: str, reconnect_seconds: float = 5):
        self.url = int(url) if url.isdigit() else url
        self.reconnect_seconds = reconnect_seconds
        self._frame: np.ndarray | None = None
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"capture-{url}")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.url)
            if not cap.isOpened():
                log.warning("Cannot open %s; retrying in %ss", self.url, self.reconnect_seconds)
                self._stop.wait(self.reconnect_seconds)
                continue
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    log.warning("Stream %s dropped; reconnecting", self.url)
                    break
                with self._lock:
                    self._frame, self._frame_time = frame, time.monotonic()
            cap.release()
            self._stop.wait(1)

    def read(self) -> np.ndarray | None:
        with self._lock:
            frame, self._frame = self._frame, None  # each frame is analysed at most once
            return frame

    def close(self) -> None:
        self._stop.set()


class FileSource:
    """Plays a recorded video at real-time pace, for testing zones and thresholds on footage."""

    def __init__(self, path: str, sample_seconds: float, loop: bool = True):
        self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise ValueError(f"Cannot open video {path}")
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 25
        self.skip = max(1, int(round(fps * sample_seconds)))
        self.loop = loop

    def read(self) -> np.ndarray | None:
        for _ in range(self.skip - 1):
            self.cap.grab()
        ok, frame = self.cap.read()
        if not ok and self.loop:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
        return frame if ok else None

    def close(self) -> None:
        self.cap.release()


def open_source(camera: Camera, sample_seconds: float) -> FrameSource:
    src = camera.source
    if src.isdigit() or "://" in src:
        return LiveSource(src)
    return FileSource(src, sample_seconds)


class Monitor:
    def __init__(
        self,
        settings: Settings,
        detector: Detector,
        policy: AlertPolicy,
        source_factory: Callable[[Camera], FrameSource] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.detector = detector
        self.policy = policy
        self.clock = clock
        self.source_factory = source_factory or (lambda cam: open_source(cam, settings.sample_seconds))
        self.trackers = {
            z.id: ZoneTracker(z, smoothing=settings.smoothing, clear_seconds=settings.clear_seconds)
            for z in settings.zones
        }
        self.sources: dict[str, FrameSource] = {}
        self.last_frame_at: dict[str, float | None] = {c.id: None for c in settings.cameras}
        self.online: dict[str, bool] = {c.id: False for c in settings.cameras}
        self._offline_alerted: dict[str, bool] = {c.id: False for c in settings.cameras}
        self.started_at = clock()
        self.snapshots: dict[str, bytes] = {}
        self._detect_lock = threading.Lock()  # ONNX sessions are shared across camera threads
        self._stop = threading.Event()

    # ---- one analysis step per camera (also what the tests drive directly) ----------------

    def process_frame(self, camera: Camera, frame: np.ndarray) -> None:
        now = self.clock()
        with self._detect_lock:
            boxes = self.detector.detect(frame)
        feet = foot_points(boxes)
        self.last_frame_at[camera.id] = now
        if not self.online[camera.id]:
            self.online[camera.id] = True
            if self._offline_alerted[camera.id]:
                self._offline_alerted[camera.id] = False
                self.policy.on_camera(camera.name, True, [z.name for z in camera.zones])
        for zone in camera.zones:
            inside = int(points_in_polygon(feet, zone.polygon).sum())
            for event in self.trackers[zone.id].update(inside, now):
                self.policy.on_zone_event(event)
        if self.settings.snapshots:
            self.snapshots[camera.id] = self._annotate(camera, frame, boxes)

    def check_health(self) -> None:
        now = self.clock()
        for cam in self.settings.cameras:
            last = self.last_frame_at[cam.id] or self.started_at
            if now - last < self.settings.offline_after_seconds:
                continue
            if self.online[cam.id] or not self._offline_alerted[cam.id]:
                self.online[cam.id] = False
                for z in cam.zones:
                    self.trackers[z.id].mark_stale()
                if not self._offline_alerted[cam.id]:
                    self._offline_alerted[cam.id] = True
                    self.policy.on_camera(cam.name, False, [z.name for z in cam.zones])
        self.policy.tick({zid: t.state for zid, t in self.trackers.items()})

    def _annotate(self, camera: Camera, frame: np.ndarray, boxes: np.ndarray) -> bytes:
        img = frame.copy()
        for x1, y1, x2, y2, _ in boxes.astype(int):
            cv2.rectangle(img, (x1, y1), (x2, y2), (255, 255, 255), 1)
        for zone in camera.zones:
            st = self.trackers[zone.id].state
            color = LEVEL_COLORS_BGR[st.level]
            pts = np.asarray(zone.polygon, dtype=np.int32)
            overlay = img.copy()
            cv2.fillPoly(overlay, [pts], color)
            img = cv2.addWeighted(overlay, 0.25, img, 0.75, 0)
            cv2.polylines(img, [pts], True, color, 2)
            x, y = pts.min(axis=0)
            cv2.putText(img, f"{zone.name}: {st.density:.1f}/m2", (int(x) + 4, int(y) + 22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return jpg.tobytes() if ok else b""

    # ---- threads ---------------------------------------------------------------------------

    def start(self) -> None:
        for cam in self.settings.cameras:
            threading.Thread(target=self._camera_loop, args=(cam,), daemon=True, name=f"cam-{cam.id}").start()
        threading.Thread(target=self._health_loop, daemon=True, name="health").start()

    def stop(self) -> None:
        self._stop.set()
        for s in self.sources.values():
            s.close()

    def _camera_loop(self, cam: Camera) -> None:
        while not self._stop.is_set():
            try:
                self.sources[cam.id] = self.source_factory(cam)
                break
            except Exception:
                log.exception("Could not open camera %s; retrying", cam.id)
                self._stop.wait(10)
        while not self._stop.is_set():
            started = time.monotonic()
            frame = self.sources[cam.id].read()
            if frame is not None:
                try:
                    self.process_frame(cam, frame)
                except Exception:
                    log.exception("Analysis failed on camera %s", cam.id)
            self._stop.wait(max(0.1, self.settings.sample_seconds - (time.monotonic() - started)))

    def _health_loop(self) -> None:
        while not self._stop.wait(2):
            try:
                self.check_health()
            except Exception:
                log.exception("Health check failed")

    # ---- dashboard view --------------------------------------------------------------------

    def status(self) -> dict:
        now = self.clock()
        cameras = []
        for cam in self.settings.cameras:
            last = self.last_frame_at[cam.id]
            cameras.append({
                "id": cam.id, "name": cam.name, "online": self.online[cam.id],
                "seconds_since_frame": round(now - last, 1) if last is not None else None,
                "zones": [{**self.trackers[z.id].state.__dict__, "area_m2": round(z.area_m2, 1),
                           "thresholds": z.thresholds.__dict__} for z in cam.zones],
            })
        return {"site": self.settings.site_name, "cameras": cameras}
