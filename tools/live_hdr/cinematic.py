#!/usr/bin/env python3
"""Cinematic processing — decision algorithm + QSVEnc runner.

The decision algorithm encodes the validated processing matrix ("Pro TENTO
klip?" column) for a concrete source clip and display capability:

  * SDR + HDR-capable pipe  -> SDR->HDR (HDRTVNet++ ONNX)   [WOW #1]
  * lossy codec (vp9/av1)   -> artifact cleanup (ArtCNN)    [WOW #2]
  * always (after the above)-> subtle CAS detail + color    [final polish]
  * frame interpolation     -> ONLY when display refresh > source fps
                              (120 Hz showroom mode; 60 Hz display -> OFF)
  * super resolution        -> ONLY when source genuinely upscales
                              (short side < 720); this 1296x2304 clip: NO
  * deband / temporal denoise -> only when measured banding/noise (default OFF)
  * tone mapping            -> only for HDR sources (this SDR clip: NO)
  * deinterlace             -> only for interlaced sources (this 60p: NO)

Methods are chained as cumulative ladder steps; each step is one or more
QSVEnc passes (ONNX VPP filters are single-instance, so stacked NN effects
run as sequential re-encode passes).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TOOLS = Path(__file__).resolve().parents[1]
QSENCC = TOOLS / "qsvenc" / "QSVEncC64.exe"
MODEL_DIR = TOOLS / "qsvenc" / "models"

# Output HDR signaling shared by HDR steps (matches the TV's HDR10 path).
HDR_MASTER_DISPLAY = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)"
HDR_MAX_CLL = "1000,400"


def video_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos"


def ffprobe(path: Path) -> dict:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,width,height,r_frame_rate,"
             "avg_frame_rate,pix_fmt,color_primaries,color_transfer,bit_rate,"
             "field_order:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        s = ((json.loads(r.stdout or "{}").get("streams") or [{}])[0])
        num, _, den = str(s.get("r_frame_rate") or "0/1").partition("/")
        fps = round(float(num) / max(1.0, float(den or 1)), 4)
        dur = float((json.loads(r.stdout or "{}").get("format") or {}).get("duration") or 0)
        return {
            "codec": s.get("codec_name") or "",
            "width": int(s.get("width") or 0),
            "height": int(s.get("height") or 0),
            "fps": fps,
            "pix_fmt": s.get("pix_fmt") or "",
            "primaries": s.get("color_primaries") or "",
            "transfer": s.get("color_transfer") or "",
            "bitrate": int(s.get("bit_rate") or 0),
            "interlaced": "Interlaced" in (s.get("field_order") or ""),
            "duration": dur,
        }
    except Exception as e:
        return {"error": str(e)}


@dataclass
class Method:
    """One matrix row: when it applies + how to run it via QSVEnc."""
    id: str
    name: str
    apply: callable            # (meta) -> bool  — decision rule
    passes: list               # list of callables (meta, pass_path) -> [args...]
    hdr_output: bool = False   # True -> 10-bit BT.2020/PQ output signaling
    reason: str = ""


def _src_is_sdr(meta: dict) -> bool:
    return (meta.get("transfer") or "").lower() not in ("smpte2084", "arib-std-b67", "pq")


def _src_lossy(meta: dict) -> bool:
    return (meta.get("codec") or "").lower() in ("vp9", "av1", "h264", "hevc")


def _short_side(meta: dict) -> int:
    return min(int(meta.get("width") or 0), int(meta.get("height") or 0))


# ── QSVEnc pass builders ────────────────────────────────────────────────
def run_qsvenc_pass(src: Path, dst: Path, vpp_args: list, hdr: bool) -> dict:
    """Run one QSVEnc pass: HEVC 10-bit; BT.2020/PQ signaling when hdr."""
    cmd = [str(QSENCC), "-i", str(src), "--avsw",
           "-c", "hevc", "--profile", "main10",
           "--output-depth", "10", "--output-csp", "yuv420",
           "--colormatrix", "bt2020nc" if hdr else "bt709",
           "--colorprim", "bt2020" if hdr else "bt709",
           "--transfer", "smpte2084" if hdr else "bt709",
           "--vpp-onnx-model-dir", str(MODEL_DIR)]
    if hdr:
        cmd += ["--max-cll", HDR_MAX_CLL, "--master-display", HDR_MASTER_DISPLAY]
    cmd += vpp_args
    cmd += ["-o", str(dst)]
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    wall = time.perf_counter() - t0
    out = (r.stdout or "") + "\n" + (r.stderr or "")
    fps = 0.0
    import re
    m = re.search(r"Encode fps\s*:\s*([\d.]+)", out)
    if m:
        fps = float(m.group(1))
    return {
        "ok": r.returncode == 0 and dst.is_file() and dst.stat().st_size > 1000,
        "wallSec": round(wall, 1),
        "fps": fps,
        "tail": out[-500:],
    }


# ── The matrix: methods + decision rules ───────────────────────────────
METHODS = [
    Method(
        id="hdr", name="SDR→HDR (HDRTVNet++)",
        apply=lambda m: _src_is_sdr(m),
        passes=[lambda m, p: ["--vpp-onnx", "model=hdrtvnetpp_agcm_stable,device=GPU,prec=fp16"]],
        hdr_output=True,
        reason="source SDR BT.709, pipe delivers PQ/BT.2020 → biggest visual win",
    ),
    Method(
        id="cleanup", name="Artifact cleanup (ArtCNN)",
        apply=lambda m: _src_lossy(m),
        # r8f64_jpeg420: 1x RGB artifact cleaner (native res, fixes chroma).
        # NOT r16f128_int8_perf — INT8 + this QSVEnc/OpenVINO path hangs on GPU
        # and CPU (prec=auto resolves f16/f32 anyway), and the non-INT8
        # R16F128/R16F96 x2-luma family grinds in OpenVINO compile (6+ GB RAM,
        # no frames). c4f16 works but is a 2x upscaler — wrong for cleanup.
        passes=[lambda m, p: ["--vpp-onnx", "model=artcnn_r8f64_jpeg420,device=GPU,prec=fp16"]],
        reason="lossy VP9 compression cleanup, not upscale (1x RGB ArtCNN)",
    ),
    Method(
        id="detail", name="Subtle detail/CAS",
        apply=lambda m: True,
        # --vpp-cas params are sharpness (0.0-1.0, default 0.40), NOT
        # "strength" — an unknown param is silently dropped and CAS would run
        # at its default 0.40. hdr=true skips the SDR gamma-2.0 luma
        # approximation (content is PQ). CAS is luma-only by design; chroma
        # sharpening stays off. 1x filter — no resolution change.
        passes=[lambda m, p: ["--vpp-cas", "sharpness=0.25,hdr=true"]],
        reason="gentle final polish (CAS luma-only, 1x)",
    ),
    Method(
        id="color", name="Color finishing (COLOR_BALANCED_V1)",
        apply=lambda m: True,
        # COLOR_BALANCED_V1 — conservative showroom grade, P2 spec:
        #   saturation +5% (spec +3..+6)  -> saturation=1.05
        #   contrast small boost          -> contrast=1.02 (PQ code values,
        #                                     also gives a small highlight gain
        #                                     above the midpoint; no SDR gamma)
        #   midtone lift / black level    -> unchanged (gamma=1.0, brightness=0)
        #   skin hue                      -> unchanged (hue=0)
        # --vpp-tweak operates on YUV code values directly (no Rec.709 gamma
        # conversion), so it is HDR/PQ-safe by construction — no SDR math on
        # PQ pixels. Deterministic, no AI. Tune later per TV verdict.
        passes=[lambda m, p: [
            "--vpp-tweak",
            "brightness=0,contrast=1.02,gamma=1.0,saturation=1.05,hue=0",
        ]],
        reason="COLOR_BALANCED_V1: sat +5%, contrast 1.02, blacks/midtones/hue untouched",
    ),
    Method(
        id="interp", name="60→120 AI frame interpolation",
        apply=lambda m: _interp_applies(m),
        passes=[lambda m, p: ["--vpp-ai-frameinterp"]],
        reason="only for >60 Hz display showroom mode",
    ),
]

# 120 Hz display is not verified on this chain yet -> interpolation stays OFF.
def _interp_applies(meta: dict) -> bool:
    return False  # gated: needs display_hz > source fps AND verified 120 Hz


def _debInt_applies(meta: dict) -> bool:
    return False  # deband/denoise: only when measurement shows banding/noise


LADDER = ["hdr", "cleanup", "detail", "color", "interp"]


def plan_for_clip(meta: dict) -> dict:
    """Decision algorithm: ordered ladder steps applicable to this clip."""
    steps = []
    for mid in LADDER:
        meth = next(x for x in METHODS if x.id == mid)
        if meth.apply(meta):
            steps.append({
                "id": mid, "name": meth.name, "hdr": meth.hdr_output,
                "reason": meth.reason,
            })
    return {
        "meta": meta,
        "steps": steps,
        "upscaleNeeded": _short_side(meta) < 720,
        "interlaced": bool(meta.get("interlaced")),
    }


def process_step(video_id: str, step_id: str, device: str = "GPU") -> dict:
    """Run the cumulative ladder up to `step_id` via sequential QSVEnc passes.

    Returns {ok, path, passes:[...], wallSec, fps}.
    """
    vd = video_root() / video_id
    src = vd / "source.mp4"
    if not src.is_file():
        return {"ok": False, "error": f"source missing: {src}"}
    meta = ffprobe(src)
    plan = plan_for_clip(meta)
    ids = [s["id"] for s in plan["steps"]]
    if step_id not in ids:
        return {"ok": False, "error": f"step '{step_id}' not applicable; ladder={ids}",
                "plan": plan}
    upto = ids.index(step_id) + 1
    active = plan["steps"][:upto]
    hdr = any(s["hdr"] for s in active)

    ed = vd / "enhanced"
    ed.mkdir(parents=True, exist_ok=True)

    def _cache_valid(dst: Path) -> bool:
        """A cached chain-prefix must be a real playable HEVC MP4, not just
        non-empty — a crash mid-pass or a failed QSVEnc run leaves a file
        that passes a size check but has no moov/stream and would poison the
        chain (and the split render that consumes it)."""
        if not dst.is_file() or dst.stat().st_size < 100_000:
            return False
        m = ffprobe(dst)
        return bool(m.get("codec") == "hevc" and m.get("width", 0) > 0
                    and m.get("height", 0) > 0 and m.get("fps", 0) > 0)

    prev = src
    passes = []
    t0 = time.perf_counter()
    # Each cin_<id>.mp4 is the cumulative chain prefix (passes chain prev →
    # dst), so any existing file up to the requested step is a valid start.
    # Reuse the deepest cached prefix and run only the missing steps — never
    # re-run an earlier pass just because a later step was requested.
    start = 0
    for i, step in enumerate(active):
        dst = ed / f"cin_{step['id']}.mp4"
        if _cache_valid(dst):
            prev = dst
            passes.append({"id": step["id"], "cached": True})
            start = i + 1
        else:
            break  # first missing step: everything from here must run
    for i in range(start, len(active)):
        step = active[i]
        meth = next(x for x in METHODS if x.id == step["id"])
        dst = ed / f"cin_{step['id']}.mp4"
        tmp = ed / f"_cin_{step['id']}_{os.getpid()}.mp4"
        args = meth.passes[0](meta, tmp)
        res = run_qsvenc_pass(prev, tmp, args, hdr=hdr or step["hdr"])
        if not res["ok"]:
            return {"ok": False, "error": f"pass {step['id']} failed",
                    "result": res, "plan": plan}
        tmp.replace(dst)
        prev = dst
        passes.append({"id": step["id"], "fps": res["fps"],
                       "wallSec": res["wallSec"]})
    return {
        "ok": True,
        "path": str(prev),
        "video_id": video_id,
        "step": step_id,
        "ladder": [s["id"] for s in active],
        "hdr": hdr,
        "passes": passes,
        "wallSec": round(time.perf_counter() - t0, 1),
        "meta": meta,
        "plan": plan,
    }


if __name__ == "__main__":
    import sys
    vid = sys.argv[1] if len(sys.argv) > 1 else "tbsY9zC-w6U"
    step = sys.argv[2] if len(sys.argv) > 2 else "hdr"
    vd = video_root() / vid
    meta = ffprobe(vd / "source.mp4")
    print(json.dumps(plan_for_clip(meta), ensure_ascii=False, indent=2))
    res = process_step(vid, step)
    print(json.dumps(res, ensure_ascii=False, indent=2))