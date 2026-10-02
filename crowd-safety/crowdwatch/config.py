"""Site configuration: cameras, zones, thresholds and who gets alerted."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .geometry import ground_area_m2, ground_homography, polygon_area

LEVELS = ["normal", "busy", "warning", "critical"]


@dataclass
class Thresholds:
    """Density thresholds in people per square metre.

    Defaults follow widely used crowd-safety guidance (Fruin levels of service, G. Keith Still):
    below ~2/m² people move freely; at 4–5/m² individuals lose control of their movement and a
    surge can turn into a crush. 'critical' is set at 4 so staff act *before* 5/m².
    For areas where the crowd is moving (stairs, ramps, corridors), use lower values.
    """

    busy: float = 2.0
    warning: float = 3.0
    critical: float = 4.0
    # Alert when density is climbing faster than this (people/m² per minute) while above 'busy'.
    rise_per_minute: float = 0.75

    def level_for(self, density: float) -> str:
        if density >= self.critical:
            return "critical"
        if density >= self.warning:
            return "warning"
        if density >= self.busy:
            return "busy"
        return "normal"

    def threshold(self, level: str) -> float:
        return {"normal": 0.0, "busy": self.busy, "warning": self.warning, "critical": self.critical}[level]


@dataclass
class ClutterSettings:
    """For zones that must stay clear (steps, exits, walkways): alert when footwear or other objects
    pile up on the floor."""

    alert_objects: int = 3  # alert when at least this many items are lying in the zone...
    alert_percent: float = 3.0  # ...or when they cover this % of its floor (a heap counts as one item)
    min_object_pixels: int = 60  # ignore specks smaller than this (raise for close-up cameras)
    alert_seconds: float = 60.0  # ...for this long before alerting (and before 'cleared')
    repeat_seconds: float = 900.0  # remind every 15 min until someone clears it
    difference: float = 0.8  # how different from the clean floor a pixel must look (brightness std units)
    persistence_rate: float = 0.15  # how quickly a patch counts as "staying put" (0..1 per sample)
    max_density_to_judge: float = 1.5  # above this people/m², the floor is too hidden to judge


@dataclass
class Zone:
    id: str
    name: str
    camera_id: str
    polygon: list[tuple[float, float]]  # image pixels, drawn on the floor area
    area_m2: float
    thresholds: Thresholds
    # Detectors undercount when people block each other. Set this from a manual-count check
    # (manual count / detected count on sample frames at peak times). See README.
    count_multiplier: float = 1.0
    keep_clear: ClutterSettings | None = None


@dataclass
class Camera:
    id: str
    name: str
    source: str  # rtsp://..., http://..., a video file path, or a webcam index like "0"
    zones: list[Zone] = field(default_factory=list)


@dataclass
class Recipient:
    name: str
    channel: str  # whatsapp | webhook | console
    address: str = ""  # phone number (digits) for whatsapp, URL for webhook
    min_level: str = "warning"  # lowest level this person is alerted for


@dataclass
class Settings:
    site_name: str
    cameras: list[Camera]
    recipients: list[Recipient]
    model_path: str = "models/yolox_s.onnx"
    detection_confidence: float = 0.35
    tiles: tuple[int, int] = (1, 1)  # split wide frames into a grid so distant heads are found
    sample_seconds: float = 2.0
    smoothing: float = 0.5  # EMA weight of the newest reading (1 = no smoothing)
    clear_seconds: float = 60.0  # density must stay lower this long before a level is downgraded
    repeat_critical_seconds: float = 120.0
    offline_after_seconds: float = 20.0
    snapshots: bool = True
    state_dir: str = "state"  # clean-floor references for keep_clear zones survive restarts here

    @property
    def zones(self) -> list[Zone]:
        return [z for c in self.cameras for z in c.zones]


class ConfigError(ValueError):
    pass


def _thresholds(raw: dict[str, Any] | None, base: Thresholds) -> Thresholds:
    merged = {**base.__dict__, **(raw or {})}
    t = Thresholds(**merged)
    if not (0 < t.busy < t.warning < t.critical):
        raise ConfigError(f"Thresholds must satisfy 0 < busy < warning < critical, got {merged}")
    return t


def _clutter(raw: Any) -> ClutterSettings | None:
    if not raw:
        return None
    if raw is True:
        return ClutterSettings()
    unknown = set(raw) - set(ClutterSettings.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"Unknown keep_clear options: {sorted(unknown)}")
    return ClutterSettings(**raw)


def load_settings(path: str | Path) -> Settings:
    raw = yaml.safe_load(Path(path).read_text())
    try:
        return parse_settings(raw)
    except (KeyError, TypeError) as e:
        raise ConfigError(f"Invalid config {path}: missing or malformed field {e}") from e


def parse_settings(raw: dict[str, Any]) -> Settings:
    defaults = _thresholds(raw.get("default_thresholds"), Thresholds())
    cameras, zone_ids = [], set()
    for cam_raw in raw["cameras"]:
        cam = Camera(id=str(cam_raw["id"]), name=cam_raw.get("name", cam_raw["id"]), source=str(cam_raw["source"]))
        homography = None
        if calib := cam_raw.get("ground_calibration"):
            homography = ground_homography(calib["image_points"], calib["ground_points_m"])
        for z in cam_raw.get("zones", []):
            polygon = [tuple(map(float, p)) for p in z["polygon"]]
            if len(polygon) < 3 or polygon_area(polygon) == 0:
                raise ConfigError(f"Zone {z['id']}: polygon needs at least 3 non-collinear points")
            if "area_m2" in z:
                area = float(z["area_m2"])
            elif homography is not None:
                area = ground_area_m2(polygon, homography)
            else:
                raise ConfigError(
                    f"Zone {z['id']}: give area_m2, or add ground_calibration to camera {cam.id}"
                )
            if area <= 0:
                raise ConfigError(f"Zone {z['id']}: area must be positive")
            if z["id"] in zone_ids:
                raise ConfigError(f"Duplicate zone id {z['id']}")
            zone_ids.add(z["id"])
            cam.zones.append(
                Zone(
                    id=str(z["id"]),
                    name=z.get("name", z["id"]),
                    camera_id=cam.id,
                    polygon=polygon,
                    area_m2=area,
                    thresholds=_thresholds(z.get("thresholds"), defaults),
                    count_multiplier=float(z.get("count_multiplier", 1.0)),
                    keep_clear=_clutter(z.get("keep_clear")),
                )
            )
        cameras.append(cam)

    recipients = []
    for r in raw.get("recipients", []):
        rec = Recipient(**r)
        if rec.channel not in ("whatsapp", "webhook", "console"):
            raise ConfigError(f"Recipient {rec.name}: unknown channel {rec.channel}")
        if rec.min_level not in LEVELS:
            raise ConfigError(f"Recipient {rec.name}: min_level must be one of {LEVELS}")
        recipients.append(rec)

    options = {k: raw[k] for k in (
        "model_path", "detection_confidence", "sample_seconds", "smoothing", "clear_seconds",
        "repeat_critical_seconds", "offline_after_seconds", "snapshots", "state_dir",
    ) if k in raw}
    if "tiles" in raw:
        options["tiles"] = tuple(raw["tiles"])
    return Settings(site_name=raw.get("site_name", "Site"), cameras=cameras, recipients=recipients, **options)
