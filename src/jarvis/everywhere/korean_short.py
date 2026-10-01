#!/usr/bin/env python3
"""Korean Short pipeline — Toastovač Whisper path (reused exactly).

Reuses the daemon's faster-whisper configuration:
  model       large-v3-turbo (mobiuslabsgmbh)  [cfg default]
  cache       D:\\_MODELS  (local-first HF cache root)
  compute     int8, device auto (CUDA probe, CPU fallback)

Outputs:
  <video_dir>/ko_transcript.json  — Korean segments with timestamps
  <video_dir>/cs_subtitles.srt    — Czech subtitles (translated)
  <video_dir>/ko_original.srt     — original Korean SRT (for reference)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

VIDEO_ROOT = Path(r"C:\Users\lukes.COREI9\AppData\Local\VIVERRA\Toastovac\videos")
MODEL_CACHE = Path(r"D:\_MODELS")
WHISPER_MODEL = "large-v3-turbo"


def video_dir(video_id: str) -> Path:
    return VIDEO_ROOT / video_id


def extract_audio(video_id: str) -> Path:
    """Extract 16 kHz mono WAV for Whisper from the downloaded source.mp4."""
    vd = video_dir(video_id)
    src = vd / "source.mp4"
    out = vd / "audio16k.wav"
    if out.is_file() and out.stat().st_size > 1000:
        return out
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(out)]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError("audio extract failed: " + r.stderr.decode(errors="replace")[-500:])
    print("AUDIO_EXTRACT", out, f"{time.perf_counter() - t0:.1f}s", flush=True)
    return out


def load_whisper():
    from jarvis.everywhere.whisper_worker import get_worker
    w = get_worker()
    w.ensure_loaded()
    print("WHISPER_LOADED", WHISPER_MODEL, "via WhisperWorker",
          f"load_count={w.load_count}", flush=True)
    return w


def transcribe_korean(video_id: str, model=None) -> dict:
    """Transcribe with explicit Korean language via the persistent worker."""
    wav = extract_audio(video_id)
    from jarvis.everywhere.whisper_worker import get_worker
    w = get_worker()
    result = w.transcribe_file(str(wav), language="ko")
    print("WHISPER_DONE", f"audio={result.get('audio_duration', 0):.1f}s "
          f"wall={result.get('wall_sec', 0):.1f}s rtf={result.get('rtf', 0):.2f} "
          f"load_count={w.load_count} inf={w.inference_count}", flush=True)
    vd = video_dir(video_id)
    (vd / "ko_transcript.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def segments_to_srt(segments, path: Path):
    def ts(sec):
        ms = int(round((sec - int(sec)) * 1000))
        s = int(sec) % 60
        m = (int(sec) // 60) % 60
        h = int(sec) // 3600
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(f"{i}")
        lines.append(f"{ts(seg['start'])} --> {ts(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    print("SRT_WRITTEN", path, len(segments), "cues", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--transcribe-only", action="store_true")
    args = ap.parse_args()
    tr = transcribe_korean(args.video_id)
    vd = video_dir(args.video_id)
    segments_to_srt(tr["segments"], vd / "ko_original.srt")
    print(json.dumps({k: tr[k] for k in ("language", "audio_duration", "wall_sec", "rtf", "model")},
                     ensure_ascii=False), flush=True)
    if not args.transcribe_only:
        sys.exit(0)


if __name__ == "__main__":
    main()