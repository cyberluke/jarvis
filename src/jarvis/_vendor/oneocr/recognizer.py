"""CRNN + CTC recognizer (vendored, modified fork of upstream
``oneocr/recognizer.py``).

Differences against upstream:
  * lazy session creation is guarded by a lock and once created a session
    is reused for the life of the process (deterministic under concurrent
    UI/voice requests);
  * the recognizer output is already log-softmax, so confidence is
    ``exp(log_probability)`` of the winning token — no second softmax.

Reference behavior: oneocr/onnx Java ``TextRecognizer.java``.
"""

from __future__ import annotations

import threading
from typing import Dict, List

import numpy as np
import onnxruntime as ort
from PIL import Image

_HEIGHT = 60
_MIN_WIDTH = 16
_STEP_DIVISOR = 4
_SPACE_INDEX = 0


class TextRecognizer:
    """Loads script recognizers and runs CTC decoding."""

    def __init__(self, models_dir, vocabs, providers=None):
        self.models_dir = models_dir
        self.vocabs = vocabs
        self.providers = providers if providers is not None else [
            "CPUExecutionProvider"]
        self.sessions: Dict[str, ort.InferenceSession] = {}
        self._lock = threading.Lock()

    def _get_session(self, lang_name: str) -> ort.InferenceSession:
        session = self.sessions.get(lang_name)
        if session is not None:
            return session
        with self._lock:
            session = self.sessions.get(lang_name)
            if session is None:
                path = (self.models_dir / "recognizers"
                        / f"recognizer_{lang_name}.onnx")
                session = ort.InferenceSession(str(path),
                                               providers=self.providers)
                self.sessions[lang_name] = session
        return session

    def is_loaded(self, lang_name: str) -> bool:
        return lang_name in self.sessions

    def recognize_line(self, crop: Image.Image, lang_name: str,
                       vocab_size: int):
        """Return ``(words_data, steps)`` for one line crop.

        ``words_data`` is a list of words; each word is a list of
        ``{'char', 'prob', 't'}`` runs with ``prob`` in [0, 1].
        """
        aspect = crop.width / crop.height
        new_width = max(_MIN_WIDTH, int(round(_HEIGHT * aspect)))
        resized_crop = crop.resize((new_width, _HEIGHT))

        rec_sess = self._get_session(lang_name)
        rec_inp = (np.array(resized_crop.convert("RGB"))
                   .astype(np.float32)[:, :, ::-1] / 255.0)
        rec_chw = np.transpose(rec_inp, (2, 0, 1))
        rec_data = np.expand_dims(rec_chw, axis=0).astype(np.float32)
        seq_lengths = np.array([new_width // _STEP_DIVISOR], dtype=np.int32)

        rec_out = rec_sess.run(None, {"data": rec_data,
                                      "seq_lengths": seq_lengths})
        logits = np.asarray(rec_out[0])              # [T, 1, V] log-softmax
        if logits.ndim == 3:
            logits = logits[:, 0, :]                 # [T, V]

        preds = np.argmax(logits, axis=-1)
        # The output is log-softmax already: probability = exp(log-prob).
        probs = np.exp(logits)

        blank = vocab_size - 1
        char_runs: List[dict] = []
        prev = -1
        vocab = self.vocabs.get(lang_name, {})
        for t in range(preds.shape[0]):
            pred = int(preds[t])
            if pred != prev and pred != blank:
                if pred == _SPACE_INDEX:
                    char = " "
                else:
                    char = vocab.get(pred, "")
                prob = float(probs[t, pred])
                char_runs.append({"char": char, "prob": prob, "t": t})
            prev = pred

        words_data: List[List[dict]] = []
        current_word: List[dict] = []
        for run in char_runs:
            if run["char"] == " " or not run["char"]:
                if current_word:
                    words_data.append(current_word)
                    current_word = []
            else:
                current_word.append(run)
        if current_word:
            words_data.append(current_word)

        return words_data, int(preds.shape[0])
