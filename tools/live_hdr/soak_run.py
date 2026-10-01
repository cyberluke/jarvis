#!/usr/bin/env python3
"""4K30/4K60 HDR soak runner (P1.4/P2.3).

Samples cast_host /cast/status plus SurfaceFlinger layer state on the box
over N seconds, verifies the run's PASS criteria, and writes
docs/autonomous/results/{fps}fps_hdr.json. Never displays configured FPS as
measured — every rate here is measured at its stage.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

CTRL = "http://127.0.0.1:8770/cast/status"
BOX = "192.168.1.122:5555"
OUT = r"D:\_SATIN_AI\Toastovac\jarvis\android\toastovac-tv\docs\autonomous\results"


def get_status() -> dict:
    with urllib.request.urlopen(CTRL, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def sf_sample() -> dict:
    try:
        out = subprocess.run(
            ["adb", "-s", BOX, "shell", "dumpsys", "SurfaceFlinger"],
            capture_output=True, text=True, timeout=40).stdout
        idx = out.find("(BLAST)#")
        block = out[idx:idx + 2500] if idx >= 0 else ""
        def grab(k: str):
            for line in block.splitlines():
                if k in line:
                    return line.strip()[:180]
            return None
        return {
            "dataspace": grab("dataspace="),
            "composition": grab("composition type="),
            "contentCrop": grab("geomContentCrop="),
            "transform": grab("geomBufferTransform="),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def main() -> None:
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    fps = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    t0 = time.time()
    rows: list[dict] = []
    sf_samples: list[dict] = []
    first_drops = None
    drops_decreased = False
    last_drops = None

    def sample_loop() -> None:
        nonlocal first_drops, drops_decreased, last_drops
        try:
            s = get_status()
        except Exception as e:  # noqa: BLE001
            rows.append({"t": round(time.time() - t0, 1), "error": str(e)})
            return
        box = s.get("box") or {}
        drops = box.get("framesDropped", 0)
        if first_drops is None:
            first_drops = drops
        if last_drops is not None and drops < last_drops:
            drops_decreased = True
        last_drops = drops
        row = {
            "t": round(time.time() - t0, 1),
            "requestedFps": s.get("requestedFps"),
            "producedFps": s.get("producedFps"),
            "encodedFps": s.get("encodedFps"),
            "sentFps": s.get("sentFps"),
            "boxOutputFps": box.get("outputFps"),
            "deadlineMisses": s.get("engineDeadlineMisses"),
            "queueDepth": s.get("engineQueueDepth"),
            "clients": s.get("clients"),
            "createCount": box.get("createCount"),
            "state": box.get("state"),
            "framesDropped": drops,
            "bytesSent": s.get("bytes_sent"),
            "idrSent": s.get("idr_sent"),
            "bsfP50Ms": (s.get("bsf") or {}).get("latencyP50Ms"),
            "bsfP95Ms": (s.get("bsf") or {}).get("latencyP95Ms"),
            "readback": s.get("readbackBytesPerFrame"),
            "orientation": (s.get("orientation") or {}).get("effectiveMode"),
        }
        rows.append(row)
        print(json.dumps(row), flush=True)
        if len(rows) in (2, 12, 18):
            sf_samples.append({"t": round(time.time() - t0, 1), **sf_sample()})

    while time.time() - t0 < dur:
        sample_loop()
        time.sleep(30)
    sf_samples.append({"t": round(time.time() - t0, 1), **sf_sample()})

    ok = [r for r in rows if r.get("producedFps") is not None]
    produced = [r["producedFps"] for r in ok]
    encoded = [r["encodedFps"] for r in ok]
    sent = [r["sentFps"] for r in ok]
    boxout = [r["boxOutputFps"] for r in ok if r.get("boxOutputFps") is not None]
    create_ok = all(r.get("createCount") == 1 for r in ok)
    readback_ok = all(r.get("readback") == 0 for r in ok)
    final_drops = last_drops if last_drops is not None else 0
    first = first_drops if first_drops is not None else 0
    drop_growth = max(0, final_drops - first)
    min_ok = bool(produced) and min(produced) >= fps - 0.5 \
        and bool(encoded) and min(encoded) >= fps - 0.5 \
        and bool(sent) and min(sent) >= fps - 0.5
    box_min = min(boxout) if boxout else 0.0
    box_avg = sum(boxout) / len(boxout) if boxout else 0.0
    box_ok = bool(boxout) and box_min >= fps - 1.5 and box_avg >= fps - 0.5
    pass_ = bool(ok) and min_ok and box_ok and create_ok and readback_ok \
        and drop_growth == 0 and not drops_decreased and len(rows) >= 5

    result = {
        "report": f"{fps}fps_hdr_soak",
        "status": "PASS" if pass_ else "FAIL",
        "durationSec": round(time.time() - t0, 1),
        "fps": {
            "requested": fps,
            "producedMin": round(min(produced), 2) if produced else None,
            "producedAvg": round(sum(produced) / len(produced), 2) if produced else None,
            "encodedMin": round(min(encoded), 2) if encoded else None,
            "encodedAvg": round(sum(encoded) / len(encoded), 2) if encoded else None,
            "sentMin": round(min(sent), 2) if sent else None,
            "sentAvg": round(sum(sent) / len(sent), 2) if sent else None,
            "boxOutputMin": round(box_min, 2) if boxout else None,
            "boxOutputAvg": round(box_avg, 2) if boxout else None,
        },
        "dropGrowth": drop_growth,
        "dropsCounterDecreased": drops_decreased,
        "decoderCreateCountMax": max(r.get("createCount", 0) for r in ok),
        "readbackBytesPerFrameMax": max(r.get("readback", -1) for r in ok),
        "orientation": rows[-1].get("orientation") if rows else None,
        "deadlineMissesTotal": (ok[-1].get("deadlineMisses") if ok else 0),
        "sfSamples": sf_samples,
        "samples": rows,
    }
    import os
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, f"{fps}fps_hdr.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    summary = {k: v for k, v in result.items() if k not in ("samples", "sfSamples")}
    print("SOAK_RESULT", json.dumps(summary, default=str), flush=True)


if __name__ == "__main__":
    main()