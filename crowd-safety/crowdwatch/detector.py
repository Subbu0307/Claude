"""Person detection with YOLOX (Apache-2.0) running on ONNX Runtime, CPU-friendly."""

from typing import Protocol

import cv2
import numpy as np

PERSON_CLASS = 0


class Detector(Protocol):
    def detect(self, frame: np.ndarray) -> np.ndarray:
        """Return an (N, 5) array of person boxes: x1, y1, x2, y2, score (frame pixels)."""


def decode_yolox(raw: np.ndarray, input_hw: tuple[int, int], strides=(8, 16, 32)) -> np.ndarray:
    """Turn YOLOX grid outputs (N, 85) into absolute cx, cy, w, h on the model input."""
    grids, expanded = [], []
    for stride in strides:
        h, w = input_hw[0] // stride, input_hw[1] // stride
        xv, yv = np.meshgrid(np.arange(w), np.arange(h))
        grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
        expanded.append(np.full((h * w, 1), stride))
    grid = np.concatenate(grids)
    stride = np.concatenate(expanded)
    out = raw.copy()
    out[:, :2] = (out[:, :2] + grid) * stride
    out[:, 2:4] = np.exp(out[:, 2:4]) * stride
    return out


def nms(boxes: np.ndarray, iou: float) -> np.ndarray:
    if len(boxes) == 0:
        return boxes
    xywh = np.column_stack([boxes[:, 0], boxes[:, 1], boxes[:, 2] - boxes[:, 0], boxes[:, 3] - boxes[:, 1]])
    keep = cv2.dnn.NMSBoxes(xywh.tolist(), boxes[:, 4].tolist(), 0.0, iou)
    return boxes[np.asarray(keep, dtype=int).reshape(-1)]


def tile_windows(width: int, height: int, cols: int, rows: int, overlap: float = 0.2):
    """Overlapping crop windows covering the frame, as (x0, y0, x1, y1)."""
    tw, th = width / (cols - (cols - 1) * overlap), height / (rows - (rows - 1) * overlap)
    for r in range(rows):
        for c in range(cols):
            x0, y0 = int(c * tw * (1 - overlap)), int(r * th * (1 - overlap))
            # The last row/column snaps to the frame edge so rounding never leaves a strip unseen.
            x1 = width if c == cols - 1 else min(width, int(x0 + tw))
            y1 = height if r == rows - 1 else min(height, int(y0 + th))
            yield x0, y0, x1, y1


class YoloxDetector:
    def __init__(
        self,
        model_path: str,
        confidence: float = 0.35,
        nms_iou: float = 0.45,
        tiles: tuple[int, int] = (1, 1),
    ):
        import onnxruntime as ort

        self.session = ort.InferenceSession(model_path, providers=ort.get_available_providers())
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.input_hw = (int(inp.shape[2]), int(inp.shape[3]))
        self.confidence = confidence
        self.nms_iou = nms_iou
        self.tiles = tiles

    def detect(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        boxes = [self._detect_window(frame, (0, 0, w, h))]
        if self.tiles != (1, 1):
            # Distant people are only a few pixels tall; running on crops as well finds them.
            for win in tile_windows(w, h, *self.tiles):
                boxes.append(_drop_cut_boxes(self._detect_window(frame, win), win, w, h))
        return nms(np.concatenate(boxes), self.nms_iou)

    def _detect_window(self, frame: np.ndarray, window: tuple[int, int, int, int]) -> np.ndarray:
        x0, y0, x1, y1 = window
        crop = frame[y0:y1, x0:x1]
        ih, iw = self.input_hw
        ratio = min(ih / crop.shape[0], iw / crop.shape[1])
        resized = cv2.resize(crop, (int(crop.shape[1] * ratio), int(crop.shape[0] * ratio)))
        padded = np.full((ih, iw, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        blob = padded.transpose(2, 0, 1)[None].astype(np.float32)

        raw = self.session.run(None, {self.input_name: blob})[0][0]
        preds = decode_yolox(raw, self.input_hw)
        scores = preds[:, 4] * preds[:, 5 + PERSON_CLASS]
        preds, scores = preds[scores >= self.confidence], scores[scores >= self.confidence]
        if len(preds) == 0:
            return np.zeros((0, 5), dtype=np.float32)
        cx, cy, bw, bh = (preds[:, i] / ratio for i in range(4))
        boxes = np.column_stack([cx - bw / 2 + x0, cy - bh / 2 + y0, cx + bw / 2 + x0, cy + bh / 2 + y0, scores])
        return nms(boxes.astype(np.float32), self.nms_iou)


def _drop_cut_boxes(boxes: np.ndarray, window, frame_w: int, frame_h: int, margin: float = 3) -> np.ndarray:
    """Discard boxes touching a tile edge that lies inside the frame: those people were cut in half
    by the crop, and the overlapping tiles or the full-frame pass see them whole."""
    x0, y0, x1, y1 = window
    cut = np.zeros(len(boxes), dtype=bool)
    if x0 > 0:
        cut |= boxes[:, 0] <= x0 + margin
    if y0 > 0:
        cut |= boxes[:, 1] <= y0 + margin
    if x1 < frame_w:
        cut |= boxes[:, 2] >= x1 - margin
    if y1 < frame_h:
        cut |= boxes[:, 3] >= y1 - margin
    return boxes[~cut]


def foot_points(boxes: np.ndarray) -> np.ndarray:
    """Where each person stands: bottom-centre of the box, which lies on the floor plane."""
    if len(boxes) == 0:
        return np.zeros((0, 2), dtype=np.float32)
    return np.column_stack([(boxes[:, 0] + boxes[:, 2]) / 2, boxes[:, 3]])
