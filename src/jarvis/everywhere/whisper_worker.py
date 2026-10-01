#!/usr/bin/env python3
"""Persistent Whisper worker — load large-v3-turbo once, queue jobs.

Reuses the Toastovač faster-whisper configuration (model name, cache root,
int8 / auto). Does not unload after each video. Does not create a second STT
stack. RTX is not the default device.
"""
from __future__ import annotations

import json
import os
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

MODEL_CACHE = Path(r"D:\_MODELS")
WHISPER_MODEL = "large-v3-turbo"


class WhisperState(str, Enum):
    UNLOADED = "UNLOADED"
    LOADING = "LOADING"
    READY = "READY"
    BUSY = "BUSY"
    FAILED = "FAILED"


class WhisperWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model = None
        self.state = WhisperState.UNLOADED
        self.load_count = 0
        self.inference_count = 0
        self.last_error = ""
        self.load_sec = 0.0
        self.jobs: List[Dict[str, Any]] = []
        self.device = "auto"
        self.compute = "int8"
        self.model_name = WHISPER_MODEL

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self.state.value,
                "model": self.model_name,
                "device": self.device,
                "compute": self.compute,
                "load_count": self.load_count,
                "inference_count": self.inference_count,
                "load_sec": round(self.load_sec, 2),
                "last_error": self.last_error,
                "jobs": list(self.jobs[-8:]),
            }

    def ensure_loaded(self) -> None:
        with self._lock:
            if self._model is not None and self.state in (
                    WhisperState.READY, WhisperState.BUSY):
                return
            if self.state == WhisperState.LOADING:
                pass
            self.state = WhisperState.LOADING
        try:
            from faster_whisper import WhisperModel
            t0 = time.perf_counter()
            model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute,
                cpu_threads=8,
                download_root=str(MODEL_CACHE) if MODEL_CACHE.is_dir() else None,
            )
            dt = time.perf_counter() - t0
            with self._lock:
                self._model = model
                self.load_count += 1
                self.load_sec = dt
                self.state = WhisperState.READY
                self.last_error = ""
            print("WHISPER_WORKER_LOADED", self.model_name,
                  f"{dt:.1f}s load_count={self.load_count}", flush=True)
        except Exception as e:
            with self._lock:
                self.state = WhisperState.FAILED
                self.last_error = str(e)
            raise

    def transcribe_file(self, path: str, language: str = "ko") -> Dict[str, Any]:
        self.ensure_loaded()
        with self._lock:
            model = self._model
            self.state = WhisperState.BUSY
        wav = _ensure_wav(path)
        t0 = time.perf_counter()
        segments, info = model.transcribe(
            wav, language=language, vad_filter=True, beam_size=5,
            word_timestamps=False,
        )
        out = []
        for seg in segments:
            out.append({
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": seg.text.strip(),
            })
        dur = time.perf_counter() - t0
        audio_sec = float(getattr(info, "duration", 0) or 0)
        rec = {
            "language": language,
            "segments": out,
            "audio_duration": audio_sec,
            "wall_sec": dur,
            "rtf": dur / max(audio_sec, 0.01),
            "model": self.model_name,
            "load_count": self.load_count,
        }
        with self._lock:
            self.inference_count += 1
            self.state = WhisperState.READY
            self.jobs.append({
                "path": path,
                "wall_sec": round(dur, 3),
                "audio_sec": round(audio_sec, 3),
                "rtf": round(rec["rtf"], 3),
                "inference": self.inference_count,
            })
        print("WHISPER_WORKER_JOB", self.inference_count,
              f"wall={dur:.2f}s rtf={rec['rtf']:.3f}", flush=True)
        return rec


def _ensure_wav(path: str) -> str:
    p = Path(path)
    if p.suffix.lower() == ".wav":
        return str(p)
    out = p.with_suffix(".16k.wav")
    if out.is_file() and out.stat().st_size > 1000:
        return str(out)
    import subprocess
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(p), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(out)]
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError("wav extract failed: " + r.stderr.decode(errors="replace")[-300:])
    return str(out)


_WORKER: Optional[WhisperWorker] = None
_WORKER_LOCK = threading.Lock()


def get_worker() -> WhisperWorker:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = WhisperWorker()
        return _WORKER
