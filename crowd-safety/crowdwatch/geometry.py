"""Polygon maths and ground-plane calibration."""

import cv2
import numpy as np

Point = tuple[float, float]


def polygon_area(points: list[Point]) -> float:
    """Shoelace formula. Works for any simple (non self-intersecting) polygon."""
    pts = np.asarray(points, dtype=float)
    x, y = pts[:, 0], pts[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def ground_homography(image_points: list[Point], ground_points_m: list[Point]) -> np.ndarray:
    """Homography mapping image pixels to metres on the ground plane, from 4 reference points.

    Pick 4 marks on the floor visible in the camera (tile corners, pillar bases, painted lines)
    and measure their real positions with a tape. They must not be collinear.
    """
    if len(image_points) != 4 or len(ground_points_m) != 4:
        raise ValueError("Ground calibration needs exactly 4 image points and 4 ground points.")
    return cv2.getPerspectiveTransform(
        np.asarray(image_points, dtype=np.float32), np.asarray(ground_points_m, dtype=np.float32)
    )


def to_ground(points: list[Point], homography: np.ndarray) -> list[Point]:
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    return [tuple(p) for p in cv2.perspectiveTransform(pts, homography).reshape(-1, 2).tolist()]


def ground_area_m2(polygon_px: list[Point], homography: np.ndarray) -> float:
    """Real floor area of an image polygon, correcting for camera perspective."""
    return polygon_area(to_ground(polygon_px, homography))


def points_in_polygon(points: np.ndarray, polygon_px: list[Point]) -> np.ndarray:
    """Boolean mask of which (N, 2) points fall inside (or on the edge of) the polygon."""
    if len(points) == 0:
        return np.zeros(0, dtype=bool)
    contour = np.asarray(polygon_px, dtype=np.float32).reshape(-1, 1, 2)
    return np.array([cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0 for x, y in points])
