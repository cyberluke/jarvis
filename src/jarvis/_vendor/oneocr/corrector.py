"""Orientation estimation (vendored fork of upstream ``oneocr/corrector.py``).

Adapted to the upgraded detector API: ``TextDetector.run`` now returns one
``DetectionMaps`` per FPN level; level 2 (the first) drives the search.
"""

from __future__ import annotations

import numpy as np


class OrientationCorrector:
    """Determines the optimal upright correction angle (0, 90, 180, 270)."""

    @staticmethod
    def estimate(image, detector, classifier) -> float:
        best_angle = 0.0
        best_quality = -9999.0

        for angle in (0.0, 90.0, 180.0, 270.0):
            candidate_img = (image if angle == 0.0
                             else image.rotate(angle, expand=True))
            levels = detector.run(candidate_img)
            maps = levels[0]
            active_pixels = np.argwhere(maps.scores_h > 0.5)
            if len(active_pixels) == 0:
                continue

            scores_active = maps.scores_h[maps.scores_h > 0.5]
            sorted_indices = np.argsort(scores_active)[::-1]

            flip_scores = []
            valid_scripts = 0

            for idx in sorted_indices[:3]:
                r, c = int(active_pixels[idx][0]), int(active_pixels[idx][1])
                stride = maps.stride
                cx, cy = c * stride + stride / 2.0, r * stride + stride / 2.0
                x1 = max(0, int((cx - 60) / maps.scale_x))
                y1 = max(0, int((cy - 20) / maps.scale_y))
                x2 = min(candidate_img.width, int((cx + 60) / maps.scale_x))
                y2 = min(candidate_img.height, int((cy + 20) / maps.scale_y))

                crop = candidate_img.crop((x1, y1, x2, y2))
                script_id, flip_score = classifier.classify(crop)
                flip_scores.append(flip_score)
                if script_id != 0:
                    valid_scripts += 1

            avg_flip = float(np.mean(flip_scores)) if flip_scores else -300.0
            quality = (valid_scripts * 1000.0) + avg_flip

            if quality > best_quality:
                best_quality = quality
                best_angle = angle

        return best_angle
