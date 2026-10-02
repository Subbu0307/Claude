"""Detects footwear (and other objects) left lying in areas that must stay clear: steps, exits, walkways.

No footwear-specific model is needed. CrowdWatch keeps a picture of the zone's floor when it is clean
and looks for patches that differ from it and *stay* different, while ignoring people (their boxes are
masked out). Footwear dropped on a step stays put; people walk on. The same method also catches bags,
flower baskets or anything else someone can trip over in a rush.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import ClutterSettings, Zone


@dataclass
class ClutterEvent:
    kind: str  # clutter | cleared
    zone: Zone
    objects: int


@dataclass
class ClutterState:
    objects: int = 0  # items lying on the floor that have stayed put
    percent: float = 0.0  # share of the zone floor they cover
    alerting: bool = False
    has_reference: bool = False
    floor_visible: bool = True  # False when the zone is too crowded to see the floor


MIN_SPREAD = 12.0  # grey levels


def _normalise(gray: np.ndarray) -> np.ndarray:
    """Blur and normalise brightness/contrast so gradual lighting changes don't look like objects."""
    g = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32)
    # Median and spread of the middle half: objects covering part of the floor barely move these,
    # unlike mean and standard deviation.
    q25, q50, q75 = np.percentile(g, (25, 50, 75))
    # Plain floors (polished granite, marble) have almost no spread; dividing by it would turn camera
    # noise into "objects". Never scale by less than a minimum spread of grey levels.
    spread = max((q75 - q25) / 1.35, MIN_SPREAD)
    return (g - q50) / spread


@dataclass
class ClutterTracker:
    zone: Zone
    settings: ClutterSettings
    state: ClutterState = field(default_factory=ClutterState)
    _rect: tuple[int, int, int, int] | None = None  # x, y, w, h of the zone in the frame
    _mask: np.ndarray | None = None  # zone polygon within the rect
    _reference: np.ndarray | None = None
    _score: np.ndarray | None = None  # per-pixel persistence of "changed", 0..1
    _above_since: float | None = None
    _below_since: float | None = None
    _last_alert: float | None = None

    def _prepare(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        pts = np.asarray(self.zone.polygon, dtype=np.int32)
        x, y, rw, rh = cv2.boundingRect(pts)
        x, y = max(0, x), max(0, y)
        rw, rh = min(rw, w - x), min(rh, h - y)
        self._rect = (x, y, rw, rh)
        self._mask = np.zeros((rh, rw), dtype=np.uint8)
        cv2.fillPoly(self._mask, [pts - [x, y]], 1)

    def _crop(self, frame: np.ndarray) -> np.ndarray:
        x, y, w, h = self._rect
        return cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)

    def set_reference(self, frame: np.ndarray) -> None:
        """Remember the floor as it looks now, clean. Staff do this after clearing the area."""
        if self._rect is None:
            self._prepare(frame)
        self._reference = _normalise(self._crop(frame))
        self._score = np.zeros_like(self._reference)
        self.state = ClutterState(has_reference=True)
        self._above_since = self._below_since = None

    def load_reference(self, reference: np.ndarray) -> None:
        """Restore a clean-floor reference saved before a restart (zone geometry is set on first frame)."""
        self._reference = reference.astype(np.float32)
        self._score = np.zeros_like(self._reference)
        self.state = ClutterState(has_reference=True)

    def reference_image(self) -> np.ndarray | None:
        return self._reference

    def update(self, frame: np.ndarray, person_boxes: np.ndarray, people_in_zone: int, density: float,
               now: float) -> list[ClutterEvent]:
        s = self.settings
        if self._rect is None:
            self._prepare(frame)
            if self._reference is not None and self._reference.shape != self._mask.shape:
                self._reference = None  # saved for a different camera resolution or zone outline
                self.state = ClutterState()
        if self._reference is None:
            # Learn the clean floor automatically the first time the zone is empty.
            if people_in_zone == 0:
                self.set_reference(frame)
            return []

        # Pixels hidden behind people can't be judged this round: keep their previous score.
        x, y, w, h = self._rect
        occluded = np.zeros((h, w), dtype=np.uint8)
        for bx1, by1, bx2, by2, *_ in person_boxes.astype(int):
            pad = int(0.15 * (bx2 - bx1))
            cv2.rectangle(occluded, (bx1 - x - pad, by1 - y - pad), (bx2 - x + pad, by2 - y + pad), 1, -1)
        visible = (self._mask == 1) & (occluded == 0)
        visible_share = visible.sum() / max(1, self._mask.sum())
        self.state.floor_visible = visible_share >= 0.3 and density < s.max_density_to_judge
        if not self.state.floor_visible:
            # Too crowded to see the floor: the density alerts are what matter now.
            return []

        current = _normalise(self._crop(frame))
        changed = (np.abs(current - self._reference) > s.difference).astype(np.uint8)
        changed = cv2.morphologyEx(changed, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
        alpha = s.persistence_rate
        self._score[visible] = (1 - alpha) * self._score[visible] + alpha * changed[visible]

        persistent = ((self._score > 0.6) & (self._mask == 1)).astype(np.uint8)
        # An object on a patterned floor shows up as several fragments: join nearby ones before counting.
        joined = cv2.morphologyEx(persistent, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
        n, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
        objects = int((stats[1:, cv2.CC_STAT_AREA] >= s.min_object_pixels).sum()) if n > 1 else 0
        self.state.objects = objects
        self.state.percent = round(float(100 * persistent.sum() / max(1, self._mask.sum())), 1)

        if people_in_zone == 0 and objects == 0 and changed[self._mask == 1].mean() < 0.005:
            # Empty and clean: let the reference follow slow lighting changes (sun moving, lamps on).
            # Only unchanged pixels are blended, so passing objects never get absorbed as "floor".
            steady = changed == 0
            self._reference[steady] = 0.95 * self._reference[steady] + 0.05 * current[steady]

        events: list[ClutterEvent] = []
        if objects >= s.alert_objects or (objects and self.state.percent >= s.alert_percent):
            self._below_since = None
            self._above_since = self._above_since if self._above_since is not None else now
            due = self._last_alert is None or now - self._last_alert >= s.repeat_seconds
            if now - self._above_since >= s.alert_seconds and (not self.state.alerting or due):
                self.state.alerting = True
                self._last_alert = now
                events.append(ClutterEvent("clutter", self.zone, objects))
        else:
            self._above_since = None
            if self.state.alerting and objects == 0:
                self._below_since = self._below_since if self._below_since is not None else now
                if now - self._below_since >= s.alert_seconds:
                    self.state.alerting = False
                    self._last_alert = None
                    events.append(ClutterEvent("cleared", self.zone, objects))
        return events

    def overlay(self, img: np.ndarray) -> None:
        """Paint persistent clutter magenta on an annotated frame."""
        if self._score is None:
            return
        x, y, w, h = self._rect
        persistent = (self._score > 0.6) & (self._mask == 1)
        roi = img[y:y + h, x:x + w]
        roi[persistent] = (0.4 * roi[persistent] + 0.6 * np.array([255, 0, 255])).astype(np.uint8)
