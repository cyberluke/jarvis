#!/usr/bin/env python3
"""Thin Toastovač adapter for IntelVideoEngine.exe.

Does not reimplement enhancement. NVIDIA/CUDA/NVENC/RTX are never selected.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

ENGINE_CANDIDATES = [
    Path(r"D:\_SATIN_AI\Toastovac\jarvis\tools\intel-video\IntelVideoEngine.exe"),
    Path(r"D:\_SATIN_AI\IntelEngine\engine\publish\IntelVideoEngine.exe"),
]


def engine_exe() -> Path:
    env = os.environ.get("INTEL_VIDEO_ENGINE_EXE")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise FileNotFoundError(f"INTEL_VIDEO_ENGINE_EXE set but missing: {p}")
    for p in ENGINE_CANDIDATES:
        if p.is_file():
            return p
    raise FileNotFoundError("IntelVideoEngine.exe not deployed")


def _run(args: list[str], timeout: int = 1800) -> Dict[str, Any]:
    exe = engine_exe()
    env = os.environ.copy()
    env.setdefault("INTEL_VIDEO_ENGINE_ROOT", str(Path(r"D:\_SATIN_AI\IntelEngine")))
    r = subprocess.run(
        [str(exe), *args, "--json"],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    text = (r.stdout or "").strip() or (r.stderr or "").strip()
    try:
        data = json.loads(text[text.find("{") :])
    except Exception:
        data = {"status": "error", "raw": text[-2000:], "exit": r.returncode}
    if data.get("nvidiaUsed") is True:
        raise RuntimeError("IntelVideoEngine reported nvidiaUsed=true — rejected")
    return data


def probe() -> Dict[str, Any]:
    return _run(["probe"], timeout=60)


def status() -> Dict[str, Any]:
    return _run(["status"], timeout=60)


def enhance(input_path: str, output_path: str, preset: str = "balanced",
            feature: str = "auto") -> Dict[str, Any]:
    return _run(
        ["enhance", "-i", input_path, "-o", output_path,
         "--preset", preset, "--feature", feature],
        timeout=3600,
    )
