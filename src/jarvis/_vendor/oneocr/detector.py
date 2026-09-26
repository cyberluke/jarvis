"""FPN text-line detector (vendored, upgraded fork of upstream
``oneocr/detector.py``).

Differences against upstream:
  * consumes all three FPN levels (2, 3, 4) instead of only level 2;
  * builds real quadrilateral line geometry from the eight
    ``bbox_deltas_*`` channels (four ``(x, y)`` corners per cell) instead
    of fabricated axis-aligned rectangles;
  * deterministic cross-level merge: candidates ordered by descending
    confidence, a line is suppressed when its overlap covers more than
    30 % of the smaller line's area.

Reference behavior: oneocr/onnx Java ``TextDetector.java``,
``DetectionMaps.java``, ``LineSegmenter.java``, ``LineShapes.java``,
``LevelMerge.java`` (reimplemented, not copied).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
import onnxruntime as ort
from PIL import Image

LEVELS: Tuple[int, ...] = (2, 3, 4)
_MULTIPLE = 32
_MIN_SIDE = 6.0
_MIN_PIXELS = 5
_OVERLAP_RATIO = 0.3
_ANCHOR_FACTOR = 8

# 8-neighbour order matching the link-score channel order.
_DY = (-1, -1, -1, 0, 1, 1, 1, 0)
_DX = (-1, 0, 1, 1, 1, 0, -1, -1)


@dataclass
class DetectionMaps:
    scores_h: np.ndarray          # [H, W]
    scores_v: np.ndarray          # [H, W]
    links_h: np.ndarray           # [8, H, W]
    links_v: np.ndarray           # [8, H, W]
    deltas_h: np.ndarray          # [8, H, W]
    deltas_v: np.ndarray          # [8, H, W]
    stride: float                 # target_width / feature_map_cols
    scale_x: float                # target_width / original_width
    scale_y: float                # target_height / original_height

    @property
    def anchor(self) -> float:
        return self.stride * _ANCHOR_FACTOR

    def centre_x(self, col: int) -> float:
        return col * self.stride + self.stride / 2.0

    def centre_y(self, row: int) -> float:
        return row * self.stride + self.stride / 2.0


@dataclass
class LineShape:
    quad: Tuple[float, ...]           # 8 floats: x1,y1,x2,y2,x3,y3,x4,y4
    bounds: Tuple[float, float, float, float]  # x, y, w, h
    score: float


def _fit(value: float) -> int:
    return max(_MULTIPLE, int(round(value / _MULTIPLE)) * _MULTIPLE)


class TextDetector:
    """Handles text region detection using the FPN ONNX model."""

    def __init__(self, model_path, providers=None):
        if providers is None:
            providers = ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(str(model_path), providers=providers)
        self._output_index = {
            out.name: i for i, out in enumerate(self.sess.get_outputs())
        }

    def run(self, image: Image.Image, max_side: int = 1536) -> List[DetectionMaps]:
        """Run the FPN network once and return one map set per level."""
        orig_w, orig_h = image.size
        scale = min(max_side / max(orig_w, orig_h), 1.0)
        target_w = _fit(orig_w * scale)
        target_h = _fit(orig_h * scale)

        resized_img = image.resize((target_w, target_h))
        img_np = np.array(resized_img.convert("RGB")).astype(np.float32)
        chw = np.transpose(img_np, (2, 0, 1))
        inp = np.expand_dims(chw, axis=0).astype(np.float32)
        im_info = np.array([[float(target_h), float(target_w), 1.0]],
                           dtype=np.float32)

        outputs = self.sess.run(None, {"data": inp, "im_info": im_info})
        scale_x = target_w / orig_w
        scale_y = target_h / orig_h

        levels: List[DetectionMaps] = []
        for level in LEVELS:
            scores_h = np.asarray(outputs[self._output_index[
                f"scores_hori_fpn{level}"]])
            cols = scores_h.shape[-1]
            stride = float(target_w) / float(cols)
            levels.append(DetectionMaps(
                scores_h=scores_h.reshape(scores_h.shape[-2:]),
                scores_v=np.asarray(outputs[self._output_index[
                    f"scores_vert_fpn{level}"]]).reshape(
                        scores_h.shape[-2:]),
                links_h=np.asarray(outputs[self._output_index[
                    f"link_scores_hori_fpn{level}"]]).reshape(
                        (8,) + scores_h.shape[-2:]),
                links_v=np.asarray(outputs[self._output_index[
                    f"link_scores_vert_fpn{level}"]]).reshape(
                        (8,) + scores_h.shape[-2:]),
                deltas_h=np.asarray(outputs[self._output_index[
                    f"bbox_deltas_hori_fpn{level}"]]).reshape(
                        (8,) + scores_h.shape[-2:]),
                deltas_v=np.asarray(outputs[self._output_index[
                    f"bbox_deltas_vert_fpn{level}"]]).reshape(
                        (8,) + scores_h.shape[-2:]),
                stride=stride,
                scale_x=scale_x,
                scale_y=scale_y,
            ))
        return levels

    @staticmethod
    def vertical_layout(maps: DetectionMaps, threshold: float = 0.5) -> bool:
        """True when the vertical score map beats the horizontal one."""
        return (int(np.count_nonzero(maps.scores_v > threshold))
                > int(np.count_nonzero(maps.scores_h > threshold)))

    @staticmethod
    def segment(maps: DetectionMaps, vertical: bool,
                score_threshold: float = 0.5,
                link_threshold: float = 0.0) -> List[LineShape]:
        """Connected-component line segmentation on one FPN level.

        Active cells: score > ``score_threshold``. Cells merge through the
        8-neighbour link channel when ``link > link_threshold``. Components
        with fewer than five active cells are ignored.
        """
        scores = maps.scores_v if vertical else maps.scores_h
        links = maps.links_v if vertical else maps.links_h
        deltas = maps.deltas_v if vertical else maps.deltas_h

        h, w = scores.shape
        parent = np.arange(h * w, dtype=np.int64)
        active = scores.reshape(-1) > score_threshold

        def find(i: int) -> int:
            root = i
            while parent[root] != root:
                root = parent[root]
            while parent[i] != root:
                parent[i], i = root, parent[i]
            return int(root)

        for r in range(h):
            for c in range(w):
                idx = r * w + c
                if not active[idx]:
                    continue
                for n in range(8):
                    nr, nc = r + _DY[n], c + _DX[n]
                    if nr < 0 or nr >= h or nc < 0 or nc >= w:
                        continue
                    nidx = nr * w + nc
                    if not active[nidx]:
                        continue
                    if links[n, r, c] > link_threshold:
                        ra, rb = find(idx), find(nidx)
                        if ra != rb:
                            parent[ra] = rb

        groups: dict[int, List[int]] = {}
        for i in range(h * w):
            if not active[i]:
                continue
            root = find(i)
            groups.setdefault(root, []).append(i)

        anchor = maps.anchor
        shapes: List[LineShape] = []
        for cells in groups.values():
            if len(cells) < _MIN_PIXELS:
                continue
            corners: dict[int, Tuple[float, ...]] = {}
            score_sum = 0.0
            min_x = min_y = float("inf")
            max_x = max_y = float("-inf")
            for index in cells:
                r, c = divmod(index, w)
                cx = maps.centre_x(c)
                cy = maps.centre_y(r)
                quad = [0.0] * 8
                for k in range(4):
                    qx = (cx + float(deltas[2 * k, r, c]) * anchor) / maps.scale_x
                    qy = (cy + float(deltas[2 * k + 1, r, c]) * anchor) / maps.scale_y
                    quad[2 * k] = qx
                    quad[2 * k + 1] = qy
                    if qx < min_x:
                        min_x = qx
                    if qx > max_x:
                        max_x = qx
                    if qy < min_y:
                        min_y = qy
                    if qy > max_y:
                        max_y = qy
                corners[index] = tuple(quad)
                score_sum += float(scores[r, c])

            bw = max_x - min_x
            bh = max_y - min_y
            if bw < _MIN_SIDE or bh < _MIN_SIDE:
                continue

            ordered = sorted(
                cells,
                key=(lambda i: maps.centre_y(i // w)) if vertical
                else (lambda i: maps.centre_x(i % w)),
            )
            first = corners[ordered[0]]
            last = corners[ordered[-1]]
            if vertical:
                quad = (first[0], first[1], first[2], first[3],
                        last[4], last[5], last[6], last[7])
            else:
                quad = (first[0], first[1], last[2], last[3],
                        last[4], last[5], first[6], first[7])

            shapes.append(LineShape(
                quad=quad,
                bounds=(min_x, min_y, bw, bh),
                score=score_sum / len(cells),
            ))
        return shapes

    @staticmethod
    def merge_levels(per_level: List[List[LineShape]]) -> List[LineShape]:
        """Merge detections from all FPN levels deterministically.

        Candidates are taken in descending confidence order; a candidate is
        suppressed when its overlap with an already accepted line covers
        more than 30 % of the smaller line's area.
        """
        all_shapes = [s for level in per_level for s in level]
        all_shapes.sort(key=lambda s: s.score, reverse=True)

        kept: List[LineShape] = []
        for candidate in all_shapes:
            clash = False
            for accepted in kept:
                if _containment(candidate.bounds, accepted.bounds) > _OVERLAP_RATIO:
                    clash = True
                    break
            if not clash:
                kept.append(candidate)
        return kept

    def get_segmented_lines(self, image: Image.Image,
                            score_threshold: float = 0.5,
                            link_threshold: float = 0.0,
                            max_side: int = 1536) -> List[LineShape]:
        """One-shot detection: run, segment every level, merge, sort."""
        levels = self.run(image, max_side=max_side)
        vertical = self.vertical_layout(levels[0], score_threshold)
        per_level = [self.segment(m, vertical, score_threshold, link_threshold)
                     for m in levels]
        shapes = self.merge_levels(per_level)
        shapes.sort(key=(lambda s: s.bounds[0]) if vertical
                    else (lambda s: s.bounds[1]),
                    reverse=vertical)
        return shapes


def _containment(a: Tuple[float, float, float, float],
                 b: Tuple[float, float, float, float]) -> float:
    """Overlap area over the smaller box's area (0 when disjoint)."""
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[0] + a[2], b[0] + b[2])
    bottom = min(a[1] + a[3], b[1] + b[3])
    if right <= left or bottom <= top:
        return 0.0
    overlap = (right - left) * (bottom - top)
    smaller = min(a[2] * a[3], b[2] * b[3])
    return 0.0 if smaller <= 0 else overlap / smaller
