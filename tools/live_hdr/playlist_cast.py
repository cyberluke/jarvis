#!/usr/bin/env python3
"""Toastovač playlist driver — rotates fun Asian shorts on the TV via the
cast_host HTTP API (:8770). Each video loops forever in the cast pipeline,
so this driver dwells on a video for a target duration, then casts the next.

Usage:
  python playlist_cast.py                 # curated fun playlist, once
  python playlist_cast.py --loop          # keep rotating forever
  python playlist_cast.py --ids ID1 ID2   # custom video ids
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

CTRL = "http://127.0.0.1:8770/cast"

# Curated fun Asian shorts (verified resolvable 2026-09-29, durations in sec).
CURATED = [
    ("E5PaVbozTq0", "Korean comedy (ENG sub)", 136),
    ("2Yz0aWxlpQ8", "Junya funny TikTok", 207),
    ("TLmysah-yks", "Zhouzhou funny model training", 494),
    ("r9XIeFfQt-8", "Laughter challenge", 593),
    ("6KOGdcg_ZJU", "Elevator prank", 696),
    ("t-UZpBGWcFI", "Korea's famous comedy", 150),
    ("7mT5x8kt3Gs", "Byeongmat dub", 91),
]

# Dwell = video duration, but never less than 4 min (so a short clip isn't
# churned too fast) and never more than 12 min (so the playlist stays fresh).
MIN_DWELL = 240.0
MAX_DWELL = 720.0


def post(action: str, **kw) -> dict:
    body = json.dumps({"action": action, **kw}).encode("utf-8")
    req = urllib.request.Request(CTRL, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def status() -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8770/cast/status",
                                    timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def wait_playing(video_id: str, timeout_s: float = 1500.0) -> bool:
    """Wait until the box shows this video actually playing."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        st = status()
        if st.get("activeSession") == video_id and st.get("producer_alive"):
            return True
        if st.get("lastError") and "ended" not in st.get("lastError", ""):
            pass  # a stale error from the previous loop is fine
        time.sleep(5)
    return False


def dwell_seconds(dur: float | None) -> float:
    if not dur or dur <= 0:
        return 360.0
    return max(MIN_DWELL, min(MAX_DWELL, float(dur) + 30.0))


def play_once(items: list, index: int, loop: bool) -> int:
    """Cast one item and dwell. Returns the next index (or 0 if loop)."""
    video_id, label, dur = items[index]
    print(f"PLAYLIST cast {video_id} {label} dur={dur}", flush=True)
    try:
        r = post("cast.youtube", video_id=video_id, intent="PLAY_NOW")
        print(f"PLAYLIST resp {json.dumps(r, ensure_ascii=False)}", flush=True)
    except Exception as e:
        print(f"PLAYLIST cast error: {e}", flush=True)
    if not wait_playing(video_id):
        print(f"PLAYLIST timeout waiting for {video_id}", flush=True)
    else:
        print(f"PLAYLIST playing {video_id}", flush=True)
    d = dwell_seconds(dur)
    print(f"PLAYLIST dwell {d:.0f}s on {video_id}", flush=True)
    # Sleep in small chunks so the script stays responsive to Ctrl-C.
    end = time.time() + d
    while time.time() < end:
        time.sleep(10)
    return (index + 1) % len(items) if loop else index + 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="rotate forever")
    ap.add_argument("--ids", nargs="*", default=[], help="custom video ids")
    ap.add_argument("--start", type=int, default=0)
    args = ap.parse_args()

    if args.ids:
        items = [(vid, vid, 360.0) for vid in args.ids]
    else:
        items = CURATED

    i = args.start
    while True:
        if i >= len(items):
            if not args.loop:
                break
            i = 0
        i = play_once(items, i, args.loop)
        if not args.loop and i >= len(items):
            break


if __name__ == "__main__":
    main()