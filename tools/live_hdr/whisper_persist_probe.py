#!/usr/bin/env python3
"""P1: three jobs through ONE loaded WhisperWorker."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from jarvis.everywhere.whisper_worker import get_worker

ART = Path(__file__).resolve().parents[2] / "android" / "toastovac-tv" / "docs" / "autonomous"
RESULTS = ART / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

WAV = Path(r"C:\Users\lukes.COREI9\AppData\Local\VIVERRA\Toastovac\videos\x6uD7GeAj84\audio16k.wav")


def main():
    w = get_worker()
    t0 = time.perf_counter()
    w.ensure_loaded()
    load_wall = time.perf_counter() - t0
    jobs = []
    for i in range(3):
        rec = w.transcribe_file(str(WAV), language="ko")
        jobs.append({
            "n": i + 1,
            "wall_sec": rec["wall_sec"],
            "rtf": rec["rtf"],
            "segs": len(rec["segments"]),
            "load_count_after": w.load_count,
            "inference_count": w.inference_count,
        })
    st = w.status()
    result = {
        "scenario": "whisper_persistence",
        "status": "pass" if w.load_count == 1 and w.inference_count >= 3 else "fail",
        "model": st["model"],
        "device": st["device"],
        "compute": st["compute"],
        "load_count": w.load_count,
        "inference_count": w.inference_count,
        "ensure_loaded_wall_sec": round(load_wall, 2),
        "worker_load_sec": st["load_sec"],
        "jobs": jobs,
        "production_baseline": "faster-whisper large-v3-turbo int8 auto (CPU-capable)",
        "rtx_used": False,
    }
    (RESULTS / "whisper_persistence_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
