#!/usr/bin/env python3
"""TEMP_DIAGNOSTIC_* — capture exact hevc_qsv live encoder output to a file.

Replicates the exact ffmpeg command from server.py and writes raw Annex-B
stdout to <out> continuously. No socket. Used to validate the emitted
bitstream offline. Feed N preroll frames as fast as the encoder consumes.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from hdr_scene import W, H, render_frame  # noqa: E402

FPS = 15
BITRATE = "18M"
MAXRATE = "28M"
BUFSIZE = "40M"
N_FRAMES = 60  # frames to feed


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("live_capture.hevc")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "pipe:0",
        "-vf", "format=p010le",
        "-c:v", "hevc_qsv",
        "-profile:v", "main10",
        "-preset", "veryfast",
        "-bf", "0",
        "-g", "15",
        "-forced_idr", "1",
        "-async_depth", "2",
        "-b:v", BITRATE, "-maxrate", MAXRATE, "-bufsize", BUFSIZE,
        "-color_primaries", "bt2020", "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc", "-color_range", "tv",
        "-f", "hevc",
        "-bsf:v", "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
        "pipe:1",
    ]
    print("CMD", " ".join(cmd), flush=True)
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, bufsize=0)

    def drain():
        with out.open("wb") as f:
            while True:
                chunk = enc.stdout.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                f.flush()

    th = threading.Thread(target=drain, daemon=True)
    th.start()
    t0 = time.perf_counter()
    frames = [render_frame(i / FPS, {"pre": i, "pq": "1"}).tobytes() for i in range(30)]
    print("PREROLL done", f"{time.perf_counter() - t0:.1f}s", flush=True)
    t1 = time.perf_counter()
    n = 0
    for i in range(N_FRAMES):
        try:
            enc.stdin.write(frames[i % len(frames)])
        except BrokenPipeError:
            print("PIPE BROKEN at frame", i, flush=True)
            break
        n += 1
    print("FEED done", n, "frames in", f"{time.perf_counter() - t1:.1f}s", flush=True)
    enc.stdin.close()
    try:
        enc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        enc.kill()
        enc.wait()
    th.join(timeout=5)
    err = enc.stderr.read().decode(errors="replace")
    if err.strip():
        print("FFSTDERR:\n" + err[-3000:], flush=True)
    print("OUT", out, out.stat().st_size, "bytes", flush=True)


if __name__ == "__main__":
    main()