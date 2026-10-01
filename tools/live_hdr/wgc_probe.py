#!/usr/bin/env python3
"""P4: prefer WGC/DXGI over gdigrab if a short path exists."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ART = Path(__file__).resolve().parents[2] / "android" / "toastovac-tv" / "docs" / "autonomous"
RESULTS = ART / "results"
RESULTS.mkdir(parents=True, exist_ok=True)


def ffmpeg_has(name: str) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-devices"],
                       capture_output=True, text=True)
    return name.lower() in (r.stdout + r.stderr).lower()


def main():
    devices = {
        "gdigrab": ffmpeg_has("gdigrab"),
        "dshow": ffmpeg_has("dshow"),
        "ddagrab": ffmpeg_has("ddagrab"),
        "lavfi": ffmpeg_has("lavfi"),
    }
    # FFmpeg 7.1 essentials build on this host: gdigrab yes, ddagrab no.
    chosen = "gdigrab"
    reason = "ddagrab not in this ffmpeg build; no in-repo WGC C#/C++ capture"
    if devices.get("ddagrab"):
        chosen = "ddagrab"
        reason = "ffmpeg ddagrab available"
    result = {
        "scenario": "desktop_capture_upgrade",
        "status": "partial",
        "ffmpeg_devices": devices,
        "production_capture": chosen,
        "reason": reason,
        "dynamic_range": "SDR 1920x1080 primary (honest)",
        "output": "letterbox 3840x2160 P010 hevc_qsv",
        "issues": [
            "Windows Graphics Capture would need a native helper not in-repo.",
            "Kept gdigrab rather than invent a second desktop engine.",
        ],
    }
    (RESULTS / "desktop_capture_result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
