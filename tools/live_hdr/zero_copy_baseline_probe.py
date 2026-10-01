#!/usr/bin/env python3
"""P1 baseline: precisely measure the CURRENT GPU scene frame path.

Replicates the production scene pipeline exactly (gpu_scene GLScene →
rgb48 bytes → ffmpeg hevc_qsv → Annex-B AU collection) and records per-stage
timing over 300+ frames:

  scene_update   clock/HUD mask re-raster + uniform updates
  draw           OpenGL draw submit (fbo.use/clear/vao.render)
  readback       glReadPixels (fbo.read)
  cpu_view       numpy frombuffer/reshape (zero conversion)
  cpu_bytes      numpy .tobytes()
  pipe_write     encoder stdin write (the CPU->GPU upload proxy)
  au_interval    interval between complete AU arrivals (encode + upload +
                 pipe latency end to end)

Runs a paced 320-frame session at the production 15 fps, then a 60-frame
burst (no pacing) to capture the raw throughput wall.

Output: docs/autonomous/results/zero_copy_baseline.json
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

FPS = 15
W, H = 3840, 2160
FRAMES_PACED = 320
FRAMES_BURST = 60
BURST_LIMIT_S = 30.0


def scene_cmd() -> list:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "pipe:0",
        "-vf", "format=p010le",
        "-c:v", "hevc_qsv", "-profile:v", "main10", "-preset", "veryfast",
        "-bf", "0", "-g", "15", "-forced_idr", "1", "-async_depth", "2",
        "-aud", "1", "-adaptive_i", "0", "-scenario", "livestreaming",
        "-b:v", "18M", "-maxrate", "28M", "-bufsize", "40M",
        "-color_primaries", "bt2020", "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc", "-color_range", "tv",
        "-f", "hevc",
        "-bsf:v", "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
        "pipe:1",
    ]


def pstats(name: str, samples: list, out: dict):
    if not samples:
        return
    s = sorted(samples)
    n = len(s)
    out[name] = {
        "n": n,
        "mean_ms": round(sum(samples) / n, 4),
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(s[int(n * 0.95) - 1], 4),
        "p99_ms": round(s[int(n * 0.99) - 1], 4),
        "max_ms": round(s[-1], 4),
        "min_ms": round(s[0], 4),
    }


def run_paced(gpu, proc, out: dict):
    from gpu_scene import GLScene
    scene = GLScene()
    stages = {k: [] for k in
              ("scene_update", "draw", "readback", "cpu_view", "cpu_bytes",
               "pipe_write", "au_interval")}
    frame_ms = []
    t0 = time.perf_counter()
    n = 0
    last_au_t = None
    au_times = []
    au_bytes = 0

    # AU collector on a thread so timing is concurrent with rendering.
    import threading
    au_lock = threading.Lock()
    au_interval_samples = []
    stop = threading.Event()

    def collect():
        nonlocal au_bytes
        prev = None
        while not stop.is_set():
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            au_bytes += len(chunk)
            now = time.perf_counter()
            if prev is not None:
                with au_lock:
                    au_interval_samples.append((now - prev) * 1000.0)
            prev = now
        stop.set()

    ct = threading.Thread(target=collect, daemon=True)
    ct.start()

    while n < FRAMES_PACED:
        target = t0 + n / FPS
        now = time.perf_counter()
        if now < target:
            time.sleep(target - now)
        f0 = time.perf_counter()
        scene._update_clock()
        scene._update_hud({"live": n, "fps": FPS, "pq": "1"})
        f1 = time.perf_counter()
        scene.prog["u_t"].value = float(n / FPS)
        scene._fbo.use()
        scene._fbo.clear(0, 0, 0, 1)
        scene.vao.render(mode=scene.ctx.TRIANGLE_STRIP)
        f2 = time.perf_counter()
        data = scene._fbo.read(components=4, dtype="u2")
        f3 = time.perf_counter()
        import numpy as np
        view = np.frombuffer(data, dtype=np.uint16).reshape(H, W, 4)[:, :, :3]
        f4 = time.perf_counter()
        blob = view.tobytes()
        f5 = time.perf_counter()
        proc.stdin.write(blob)
        f6 = time.perf_counter()
        stages["scene_update"].append((f1 - f0) * 1000.0)
        stages["draw"].append((f2 - f1) * 1000.0)
        stages["readback"].append((f3 - f2) * 1000.0)
        stages["cpu_view"].append((f4 - f3) * 1000.0)
        stages["cpu_bytes"].append((f5 - f4) * 1000.0)
        stages["pipe_write"].append((f6 - f5) * 1000.0)
        frame_ms.append((f6 - f0) * 1000.0)
        n += 1
        if n % 60 == 0:
            print(f"  paced {n}/{FRAMES_PACED} frame_ms={frame_ms[-1]:.1f}", flush=True)

    proc.stdin.close()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()
    stop.set()
    ct.join(timeout=5)
    el = max(0.001, time.perf_counter() - t0)
    with au_lock:
        au_interval_samples = list(au_interval_samples)
    for k, v in stages.items():
        pstats(k, v, out)
    pstats("frame_total_paced", frame_ms, out)
    pstats("au_interval", au_interval_samples, out)
    out["paced"] = {
        "frames": n,
        "wall_s": round(el, 3),
        "effective_fps": round(n / el, 3),
        "au_bytes": au_bytes,
        "stderr_rc": proc.returncode,
    }


def run_burst(gpu, out: dict):
    """Raw throughput wall: render+readback as fast as possible (no encoder)."""
    from gpu_scene import GLScene
    scene = GLScene()
    t0 = time.perf_counter()
    n = 0
    readback_ms = []
    draw_ms = []
    while n < FRAMES_BURST and time.perf_counter() - t0 < BURST_LIMIT_S:
        f0 = time.perf_counter()
        scene.prog["u_t"].value = float(n / 30.0)
        scene._fbo.use()
        scene._fbo.clear(0, 0, 0, 1)
        scene.vao.render(mode=scene.ctx.TRIANGLE_STRIP)
        f1 = time.perf_counter()
        scene._fbo.read(components=4, dtype="u2")
        f2 = time.perf_counter()
        draw_ms.append((f1 - f0) * 1000.0)
        readback_ms.append((f2 - f1) * 1000.0)
        n += 1
    el = max(0.001, time.perf_counter() - t0)
    pstats("draw_unpaced", draw_ms, out)
    pstats("readback_unpaced", readback_ms, out)
    out["burst"] = {
        "frames": n,
        "wall_s": round(el, 3),
        "render_fps": round(n / el, 3),
    }


def main():
    from gpu_scene import GLScene
    out = {"probe": "zero_copy_baseline", "frames_paced": FRAMES_PACED,
           "frames_burst": FRAMES_BURST, "resolution": f"{W}x{H}",
           "date": time.strftime("%Y-%m-%dT%H:%M:%S")}
    print("BASELINE probe: paced run (320 @ 15fps)", flush=True)
    proc = subprocess.Popen(scene_cmd(), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            bufsize=0)
    run_paced(GLScene, proc, out)
    # ffmpeg stderr tail (warnings that matter)
    try:
        err = proc.stderr.read().decode("utf-8", "replace")
        tail = [ln for ln in err.splitlines() if ln.strip()][-15:]
        out["ffmpeg_stderr_tail"] = tail
    except Exception:
        pass
    print("BASELINE probe: burst run (max throughput)", flush=True)
    run_burst(GLScene, out)
    dest = Path(__file__).resolve().parents[2] / "android" / "toastovac-tv" \
        / "docs" / "autonomous" / "results" / "zero_copy_baseline.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print("WROTE", dest, flush=True)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()