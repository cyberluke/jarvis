"""Script classifier (vendored fork of upstream ``oneocr/classifier.py``).

Outputs are resolved by name (``script_id_score`` / ``flip_score``) with a
positional fallback, so the module does not depend on output ordering.
The classifier score vector has ten entries: index 0 is "no script" and
indices 1..9 are the nine script groups (see ``common.SCRIPT_METADATA``).
"""

from __future__ import annotations

import numpy as np
import onnxruntime as ort
from PIL import Image


class ScriptClassifier:
    """Identifies the script type and orientation properties of crops."""

    def __init__(self, model_path, providers=None):
        if providers is None:
            providers = ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(str(model_path), providers=providers)
        names = [out.name for out in self.sess.get_outputs()]
        self._script_idx = (names.index("script_id_score")
                            if "script_id_score" in names else 3)
        self._flip_idx = (names.index("flip_score")
                          if "flip_score" in names else 5)

    def classify(self, crop: Image.Image) -> tuple[int, float]:
        """Return ``(script_id, flip_score)``; script_id is 1..9 (0 = none)."""
        resized = crop.resize((200, 60)).convert("RGB")
        # BGR order, normalized to [0, 1].
        cls_inp = np.array(resized).astype(np.float32)[:, :, ::-1] / 255.0
        cls_chw = np.transpose(cls_inp, (2, 0, 1))
        cls_data = np.expand_dims(cls_chw, axis=0)

        out = self.sess.run(None, {"data": cls_data})
        script_scores = np.asarray(out[self._script_idx]).flatten()
        script_id = int(np.argmax(script_scores))
        flip = np.asarray(out[self._flip_idx]).flatten()
        flip_score = float(flip[0]) if flip.size else 0.0
        return script_id, flip_score
