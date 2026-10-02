"""Turns raw per-frame counts into a stable density level and a trend, per zone."""

from collections import deque
from dataclasses import dataclass, field

from .config import LEVELS, Zone


@dataclass
class ZoneEvent:
    kind: str  # level_up | level_down | rising_fast
    zone: Zone
    level: str
    previous_level: str
    density: float
    count: int
    rise_per_minute: float
    minutes_to_critical: float | None


@dataclass
class ZoneState:
    zone_id: str
    name: str
    camera_id: str
    level: str = "normal"
    density: float = 0.0
    count: int = 0
    rise_per_minute: float = 0.0
    minutes_to_critical: float | None = None
    updated_at: float | None = None
    stale: bool = True  # no fresh reading (camera offline or not started): never shown as safe


@dataclass
class ZoneTracker:
    zone: Zone
    smoothing: float = 0.5
    clear_seconds: float = 60.0
    trend_window_seconds: float = 120.0
    _smoothed: float | None = None
    _below_since: float | None = None
    _history: deque = field(default_factory=deque)
    state: ZoneState = None  # type: ignore[assignment]

    def __post_init__(self):
        self.state = ZoneState(self.zone.id, self.zone.name, self.zone.camera_id)

    def update(self, detected_count: int, now: float) -> list[ZoneEvent]:
        t = self.zone.thresholds
        count = round(detected_count * self.zone.count_multiplier)
        raw = count / self.zone.area_m2
        self._smoothed = raw if self._smoothed is None else self.smoothing * raw + (1 - self.smoothing) * self._smoothed
        density = self._smoothed

        self._history.append((now, density))
        while self._history and now - self._history[0][0] > self.trend_window_seconds:
            self._history.popleft()
        rise = _slope_per_minute(self._history)

        events = []
        previous = self.state.level
        target = t.level_for(density)
        if LEVELS.index(target) > LEVELS.index(previous):
            # Escalate immediately: in a surge, minutes matter more than avoiding a false alarm.
            self._set_level(target, previous, density, count, rise, events, "level_up")
        elif LEVELS.index(target) < LEVELS.index(previous):
            # De-escalate only after density has stayed clearly lower for a while, so a crowd
            # hovering at a threshold doesn't flap between levels and spam staff.
            if density < t.threshold(previous) * 0.9:
                self._below_since = self._below_since if self._below_since is not None else now
                if now - self._below_since >= self.clear_seconds:
                    self._set_level(target, previous, density, count, rise, events, "level_down")
            else:
                self._below_since = None
        else:
            self._below_since = None

        eta = None
        if rise > 0 and density < t.critical:
            eta = (t.critical - density) / rise
        if rise >= t.rise_per_minute and density >= t.busy and self.state.level != "critical":
            events.append(ZoneEvent("rising_fast", self.zone, self.state.level, previous, density, count, rise, eta))

        self.state.density = round(density, 2)
        self.state.count = count
        self.state.rise_per_minute = round(rise, 2)
        self.state.minutes_to_critical = round(eta, 1) if eta is not None else None
        self.state.updated_at = now
        self.state.stale = False
        return events

    def _set_level(self, level, previous, density, count, rise, events, kind):
        self.state.level = level
        self._below_since = None
        events.append(ZoneEvent(kind, self.zone, level, previous, density, count, rise, None))

    def mark_stale(self) -> None:
        self.state.stale = True
        self._smoothed = None
        self._history.clear()


def _slope_per_minute(history: deque) -> float:
    """Least-squares slope of density over time, in people/m² per minute."""
    if len(history) < 3:
        return 0.0
    ts = [t for t, _ in history]
    ds = [d for _, d in history]
    span = ts[-1] - ts[0]
    if span < 10:
        return 0.0
    mt, md = sum(ts) / len(ts), sum(ds) / len(ds)
    var = sum((t - mt) ** 2 for t in ts)
    if var == 0:
        return 0.0
    return sum((t - mt) * (d - md) for t, d in zip(ts, ds)) / var * 60
