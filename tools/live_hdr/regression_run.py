#!/usr/bin/env python3
"""P4 focused product regression runner: native HDR scene → Korean enhanced
media → Interview Coach → native HDR scene.

Records at each step: source, decoder createCount, drops, fps, HDR dataspace
(from SurfaceFlinger), orientation. Writes
docs/autonomous/results/zero_copy_regression.json.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request

CTRL = "http://127.0.0.1:8770"
BOX = "192.168.1.122:5555"
VIDEO_ID = "x6uD7GeAj84"  # cached Korean vlog with CS subtitles
OUT = r"D:\_SATIN_AI\Toastovac\jarvis\android\toastovac-tv\docs\autonomous\results\zero_copy_regression.json"


def post(action: str, **kw) -> dict:
    body = json.dumps({"action": action, **kw}).encode("utf-8")
    req = urllib.request.Request(CTRL + "/cast", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def status() -> dict:
    with urllib.request.urlopen(CTRL + "/cast/status", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def sf_dataspace() -> str:
    try:
        out = subprocess.run(["adb", "-s", BOX, "shell", "dumpsys", "SurfaceFlinger"],
                             capture_output=True, text=True, timeout=40).stdout
        idx = out.find("(BLAST)#")
        block = out[idx:idx + 1500] if idx >= 0 else ""
        for line in block.splitlines():
            if "dataspace=" in line:
                return line.strip()[:120]
        return "no-layer"
    except Exception as e:  # noqa: BLE001
        return f"err {e}"


def wait_for(pred, timeout_s: float, label: str):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        try:
            s = status()
            last = s
            if pred(s):
                return s
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    raise TimeoutError(f"timeout waiting for {label}; last={json.dumps(last, default=str)[:300]}")


def drop_growth(seconds: float = 12.0) -> int:
    """Drops accumulated over a steady-state window (transition transients are
    bounded one-time events; SUSTAINED growth is the regression signal)."""
    a = (status().get("box") or {}).get("framesDropped", 0)
    time.sleep(seconds)
    b = (status().get("box") or {}).get("framesDropped", 0)
    return max(0, b - a)


def snapshot(s: dict, label: str, measure_growth: bool = True) -> dict:
    box = s.get("box") or {}
    return {
        "step": label,
        "source": s.get("source"),
        "requestedFps": s.get("requestedFps"),
        "producedFps": s.get("producedFps"),
        "encodedFps": s.get("encodedFps"),
        "sentFps": s.get("sentFps"),
        "boxOutputFps": box.get("outputFps"),
        "createCount": box.get("createCount"),
        "decoderState": box.get("state"),
        "framesDropped": box.get("framesDropped"),
        "dropGrowth12s": drop_growth() if measure_growth else None,
        "clients": s.get("clients"),
        "audioPkts": s.get("audio_pkts"),
        "orientation": (s.get("orientation") or {}).get("effectiveMode"),
        "sfDataspace": sf_dataspace(),
    }


def main() -> None:
    steps = []
    native_threshold = 59.0  # 60/1 requested; tolerate sample jitter at >= 59

    # 1. native HDR scene (assumed running; re-assert at the production rate)
    post("cast.scene", fpsNumerator=60, fpsDenominator=1)
    s = wait_for(lambda x: x.get("source") == "native" and x.get("clients", 0) >= 1
                 and (x.get("box") or {}).get("state") == "RUNNING", 60, "native scene")
    steps.append(snapshot(s, "1_native_scene"))

    # 2. Korean enhanced media (cached video, subtitles cached)
    post("cast.youtube", url=VIDEO_ID, intent="PLAY_NOW")
    s = wait_for(lambda x: x.get("source") == "short" and x.get("audio_pkts", 0) > 0,
                 180, "korean short playing with audio")
    # Transition snapshot (A/V lock-in may transiently drop a few frames while
    # the audio clock re-anchors; growth is measured on the STABLE step below).
    steps.append(snapshot(s, "2_korean_enhanced", measure_growth=False))
    time.sleep(10)
    s = status()
    steps.append(snapshot(s, "2_korean_stable"))

    # 3. Interview Coach
    post("cast.interviewCoach", topic="Python", durationSec=45)
    s = wait_for(lambda x: x.get("source") == "interview" and x.get("clients", 0) >= 1,
                 90, "interview coach")
    time.sleep(8)
    s = status()
    steps.append(snapshot(s, "3_interview_coach"))

    # 4. return to native HDR scene
    post("cast.scene", fpsNumerator=60, fpsDenominator=1)
    s = wait_for(lambda x: x.get("source") == "native"
                 and (x.get("box") or {}).get("state") == "RUNNING"
                 and (x.get("box") or {}).get("outputFps", 0) >= 55, 60, "native scene return")
    time.sleep(6)
    s = status()
    steps.append(snapshot(s, "4_native_return"))

    # verdicts
    def cnt(label):
        return next((x["createCount"] for x in steps if x["step"] == label), None)

    create_ok = all(x["createCount"] == 1 for x in steps if x["createCount"] is not None)
    native_ok = steps[0]["producedFps"] is not None and steps[0]["producedFps"] >= native_threshold \
        and steps[3]["producedFps"] >= native_threshold and "BT2020" in (steps[3].get("sfDataspace") or "")
    audio_ok = steps[1]["audioPkts"] > 0
    drops_ok = all(x["dropGrowth12s"] == 0 for x in steps if x["dropGrowth12s"] is not None)
    pass_ = create_ok and native_ok and audio_ok and drops_ok

    result = {
        "scenario": "zero_copy_regression_native_korean_interview",
        "status": "PASS" if pass_ else "FAIL",
        "verdicts": {
            "decoderCreateCount1": create_ok,
            "nativeFpsAndHdr": native_ok,
            "koreanAudio": audio_ok,
            "noDrops": drops_ok,
        },
        "steps": steps,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print("REGRESSION_RESULT", json.dumps({k: v for k, v in result.items() if k != "steps"},
                                          default=str), flush=True)


if __name__ == "__main__":
    main()