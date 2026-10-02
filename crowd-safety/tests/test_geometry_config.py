import copy

import numpy as np
import pytest

from crowdwatch.config import ConfigError, load_settings, parse_settings
from crowdwatch.geometry import ground_area_m2, ground_homography, points_in_polygon, polygon_area

from .conftest import BASE


def test_polygon_area():
    assert polygon_area([(0, 0), (4, 0), (4, 3), (0, 3)]) == 12
    assert polygon_area([(0, 0), (4, 0), (0, 3)]) == 6


def test_ground_area_corrects_perspective():
    # A floor rectangle 8 m wide x 10 m deep that appears as a trapezoid (far edge looks narrower).
    image = [(100, 700), (900, 700), (700, 300), (300, 300)]
    ground = [(0, 0), (8, 0), (8, 10), (0, 10)]
    h = ground_homography(image, ground)
    assert ground_area_m2(image, h) == pytest.approx(80, rel=1e-4)
    # The near half of the image trapezoid covers less than half the floor depth... in pixels it's
    # most of the area, but on the ground the far half is just as large.
    near = [(100, 700), (900, 700), (800, 500), (200, 500)]
    assert ground_area_m2(near, h) < 80


def test_points_in_polygon():
    poly = [(0, 0), (10, 0), (10, 10), (0, 10)]
    mask = points_in_polygon(np.array([[5, 5], [10, 10], [11, 5]]), poly)
    assert mask.tolist() == [True, True, False]
    assert points_in_polygon(np.zeros((0, 2)), poly).shape == (0,)


def test_example_config_loads():
    s = load_settings("site.example.yaml")
    stairs = next(z for z in s.zones if z.id == "hall_exit_stairs")
    assert stairs.thresholds.critical == 2.0 and stairs.thresholds.busy == 1.0
    gate = next(z for z in s.zones if z.id == "gate1_queue")
    assert 100 < gate.area_m2 < 140  # computed from ground calibration
    assert s.tiles == (2, 2)


@pytest.mark.parametrize("mutate, message", [
    (lambda c: c["cameras"][0]["zones"][0].pop("area_m2"), "area_m2"),
    (lambda c: c["cameras"][0]["zones"][0].update(thresholds={"warning": 5}), "busy < warning < critical"),
    (lambda c: c["cameras"][0]["zones"].append(dict(c["cameras"][0]["zones"][0])), "Duplicate"),
    (lambda c: c["recipients"].append({"name": "x", "channel": "pager"}), "unknown channel"),
    (lambda c: c["cameras"][0]["zones"][0].update(polygon=[[0, 0], [1, 1], [2, 2]]), "non-collinear"),
])
def test_config_validation(mutate, message):
    raw = copy.deepcopy(BASE)
    mutate(raw)
    with pytest.raises(ConfigError, match=message):
        parse_settings(raw)
