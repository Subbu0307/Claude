import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crowdwatch.config import parse_settings  # noqa: E402

# One camera, one 10 m x 10 m zone drawn as a 1000 x 1000 px square: 100 m², 10 px = 10 cm.
BASE = {
    "site_name": "Test Temple",
    "smoothing": 1.0,
    "clear_seconds": 30,
    "offline_after_seconds": 20,
    "repeat_critical_seconds": 120,
    "cameras": [{
        "id": "cam1", "name": "Gate cam", "source": "0",
        "zones": [{"id": "gate", "name": "Gate", "polygon": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]],
                   "area_m2": 100}],
    }],
    "recipients": [
        {"name": "Control room", "channel": "console", "min_level": "busy"},
        {"name": "Police", "channel": "console", "min_level": "critical"},
    ],
}


@pytest.fixture
def settings():
    import copy
    return parse_settings(copy.deepcopy(BASE))


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()
