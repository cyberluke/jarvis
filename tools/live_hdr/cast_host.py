#!/usr/bin/env python3
"""Toastovač Cast Host — unified live 4K HDR streamer with swappable sources.

All sources produce HEVC Annex-B → shared AU assembler → TZHL :8768
→ the single Amlogic decoder → HwcVideo DEVICE.

Sources:
  scene      live clock + Spark HDR scene (Python renderer → hevc_qsv)
  short      YouTube Short composite (ffmpeg: video + burned subtitles)
  interview  Interview Coach text scene (Python renderer → hevc_qsv)
  desktop    Windows desktop capture (ffmpeg ddagrab/gdigrab → hevc_qsv)

Control (HTTP :8770):
  POST /cast  {"action": "cast.scene" | "cast.youtube" | "cast.interviewCoach"
               | "cast.desktop" | "cast.stop", ...}
  GET  /cast/status   JSON state
"""
from __future__ import annotations

import argparse
import collections
import faulthandler
import json
import os
import queue
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Any fatal signal (access violation, abort) dumps a traceback to stderr —
# captured by the persistent log so a silent death becomes diagnosable.
faulthandler.enable()

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[1] / "src"))

from protocol import (  # noqa: E402
    AUDIO_CONFIG,
    FLAG_CONFIG,
    FLAG_FRAME,
    FLAG_IDR,
    STREAM_AUDIO,
    STREAM_VIDEO,
    pack,
)

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8768
CONTROL_PORT = 8770

# ── session FPS (P1.1: one source of truth) ─────────────────────────────
# The native scene FPS is an explicit session parameter: POST /cast
# {"action": "cast.scene", "fps": N} sets it; otherwise the default (30) or
# the TOASTOVAC_FPS env override applies. The same resolved value flows into
# toastovac_gpu.exe (--fps, --fps-den, --gop), the state-feed pacing, and the
# status endpoint. Configured FPS is never displayed as a measured FPS — status
# distinguishes requestedFps/producedFps/encodedFps/sentFps/boxOutputFps.
# Fractional rates are supported (P2.4): fps=59.94 maps to the box display
# refresh (60000/1001) so a 60.0-vs-59.94 backlog cannot accumulate.
_DISPLAY_RATE = 60000 / 1001  # 59.9400599… Hz (Samsung/Homatics active mode)
_SESSION_FPS = float(os.environ.get("TOASTOVAC_FPS", "30"))
_SESSION_NUM = 30
_SESSION_DEN = 1
_FPS_LOCK = threading.Lock()


def current_fps() -> float:
    with _FPS_LOCK:
        return _SESSION_FPS


def session_fraction() -> tuple:
    """(num, den) of the session rate — the single rational source of truth
    (P5). Kept exact: 60000/1001 stays 60000/1001, never 59.9 or 60."""
    with _FPS_LOCK:
        return _SESSION_NUM, _SESSION_DEN


def set_session_fps(fps, den=None) -> float:
    """Set the session rate. Accepts either a float (fps) or an exact rational
    (fps=num, den=den) so 60000/1001 and 60/1 are never approximated (P5)."""
    global _SESSION_FPS, _SESSION_NUM, _SESSION_DEN
    if den:
        num = max(1, int(fps))
        den = max(1, int(den))
    else:
        f = min(120.0, max(1.0, float(fps)))
        num, den = fps_fraction(f)
    with _FPS_LOCK:
        _SESSION_NUM, _SESSION_DEN = num, den
        _SESSION_FPS = num / den
    return _SESSION_FPS


def fps_fraction(fps: float) -> tuple:
    """(num, den) for the engine's frame rate. Exactly 59.94 (the box display
    refresh) becomes 60000/1001; other fractional rates get ×1000; integers
    pass through."""
    if abs(fps - _DISPLAY_RATE) < 0.01:
        return 60000, 1001
    num = round(fps * 1000)
    if num % 1000 == 0:
        return num // 1000, 1
    return num, 1000


# ── Amlogic vertical pre-flip (P0 freeze) ───────────────────────────────
# The Homatics/Amlogic 4K HEVC video plane vertically flips the native
# engine's stream (proven against ffmpeg hevc_qsv, which does not flip).
# The correction is a VERTICAL pre-flip, scoped to the live box target:
# the engine resolves its own default per display profile — pipe mode
# (this host) -> HOMATICS_AMLOGIC_VIDEO_PLANE verticalPreFlip=true; local
# --out encodes -> NONE. No env var is forced here; an explicit
# TOASTOVAC_FLIP=0|1|2 (or --flip) remains an override for A/B tests:
#   0 = none, 1 = vertical flip, 2 = rotate 180
# mode 1 is NOT "180°" — the two differ by a horizontal mirror.

STATS = {
    "frames": 0,
    "bytes": 0,
    "idr": 0,
    "config": 0,
    "audio_pkts": 0,
    "audio_bytes": 0,
    "clients": 0,
    "start": time.time(),
}
AUDIO_RATE = 48000
AUDIO_CH = 2
AUDIO_BYTES_PER_SEC = AUDIO_RATE * AUDIO_CH * 2
PCM_CHUNK = AUDIO_RATE * AUDIO_CH * 2 // 25  # 40 ms = 7680 bytes

NAL_VPS, NAL_SPS, NAL_PPS = 32, 33, 34
NAL_AUD = 35
IRAP = {16, 17, 18, 19, 20, 21, 22, 23}
VCL = set(range(0, 32))

_Q: "queue.Queue" = queue.Queue(maxsize=0)
_STATE = {
    "source": "scene",
    "detail": "",
    "producer": None,
    "producer_pid": 0,
    "started": 0.0,
    "last_error": "",
}
_STATE_LOCK = threading.Lock()


def nal_info(nal: bytes):
    p = 4 if nal.startswith(b"\x00\x00\x00\x01") else 3
    if len(nal) <= p + 1:
        return -1, False
    t = (nal[p] >> 1) & 0x3F
    first = False
    if t in VCL and len(nal) > p + 2:
        first = (nal[p + 2] & 0x80) != 0
    elif t in IRAP:
        first = True
    return t, first


class _NalAssembler:
    """Incremental Annex-B NAL splitter.

    Replaces the per-chunk full-buffer rescan: each appended chunk is
    scanned exactly once (plus a 3-byte overlap so start codes straddling a
    chunk boundary are still detected), and the pending buffer is compacted
    to the single incomplete NAL tail. Long streams fed in small pipe reads
    therefore cost O(bytes) overall instead of O(chunk * carry) — the mux
    thread pegging one core and sentFps decay on long runs was this rescan.
    """

    __slots__ = ("buf", "scan_from", "last_start", "garbage_limit")

    def __init__(self, garbage_limit: int = 8 * 1024 * 1024):
        self.buf = bytearray()
        self.scan_from = 0      # first byte not yet examined for start codes
        self.last_start = -1    # buffer index of the most recent start code
        self.garbage_limit = garbage_limit

    def reset(self) -> None:
        self.buf = bytearray()
        self.scan_from = 0
        self.last_start = -1

    @staticmethod
    def _start_code_end(buf: bytearray, at: int) -> int:
        """Byte index after the start code beginning at `at` (zero-run + 01)."""
        z = at
        n = len(buf)
        while z < n and buf[z] == 0:
            z += 1
        return z + 1 if z < n and buf[z] == 1 else z

    def feed(self, chunk: bytes) -> list:
        """Append a chunk; return complete NALs (start code included)."""
        if not chunk:
            return []
        self.buf.extend(chunk)
        out: list = []
        n = len(self.buf)
        i = self.scan_from
        last = self.last_start
        while i < n:
            if self.buf[i] != 0:
                i += 1
                continue
            j = i
            while j < n and self.buf[j] == 0:
                j += 1
            if j < n and self.buf[j] == 1 and j - i >= 2:
                if last >= 0 and i > last:
                    out.append(bytes(self.buf[last:i]))
                if i > last:
                    last = i
                i = j + 1
            else:
                i = max(i + 1, j)
        if last >= 0:
            # Compact: keep only from the most recent start code so the
            # buffer stays bounded to one partial NAL.
            if last > 0:
                del self.buf[:last]
            self.last_start = 0
            end = self._start_code_end(self.buf, 0)
            # Resume scanning after the known start code; also re-examine the
            # trailing 3 bytes so a start code straddling the next chunk
            # boundary is still detected.
            self.scan_from = max(end, len(self.buf) - 3)
        else:
            # No start code seen yet: re-examine only the tail for a
            # straddling code; drop unbounded garbage otherwise.
            if len(self.buf) > self.garbage_limit:
                del self.buf[:max(0, len(self.buf) - 3)]
                self.scan_from = 0
            else:
                self.scan_from = max(0, len(self.buf) - 3)
        return out


def scene_cmd() -> list:
    from hdr_scene import W, H
    fps = current_fps()
    gop = max(1, round(fps))
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{W}x{H}", "-r", str(fps),
        "-i", "pipe:0",
        "-vf", "format=p010le",
        "-c:v", "hevc_qsv", "-profile:v", "main10", "-preset", "veryfast",
        "-bf", "0", "-g", str(gop), "-forced_idr", "1", "-async_depth", "2",
        "-aud", "1", "-adaptive_i", "0", "-scenario", "livestreaming",
        "-b:v", "18M", "-maxrate", "28M", "-bufsize", "40M",
        "-color_primaries", "bt2020", "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc", "-color_range", "tv",
        "-f", "hevc",
        "-bsf:v", "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
        "pipe:1",
    ]


def _probe_video_dims(path: Path) -> tuple | None:
    """Return (width, height) of the first video stream, or None."""
    try:
        import subprocess as _sp
        out = _sp.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=10)
        s = ((json.loads(out.stdout).get("streams") or [{}])[0])
        w, h = s.get("width"), s.get("height")
        if w and h:
            return int(w), int(h)
    except Exception:
        pass
    return None


def _correct_ar_scale(src_path: Path, orig_w: int, orig_h: int) -> tuple | None:
    """If an enhanced derivative violates the source AR (IntelVideoEngine
    squashes portrait Shorts into a square), return (scale_expr, cw, dh) that
    restores the correct aspect before the fit/pad stage. None when fine."""
    d = _probe_video_dims(src_path)
    if not d:
        return None
    dw, dh = d
    if dw <= 0 or dh <= 0 or orig_w <= 0 or orig_h <= 0:
        return None
    if abs(dw / dh - orig_w / orig_h) < 0.02:
        return None
    cw = int(round(dh * orig_w / orig_h))
    cw -= cw % 2
    if cw <= 0 or cw >= dw:  # never invent pixels in the correction
        return None
    return (f"scale={cw}:{dh}:flags=lanczos", cw, dh)


def _derivative_for(video_id: str, preset: str) -> Path | None:
    """Locate the cached enhanced derivative for a preset (sidecar match)."""
    vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
    ed = vd / "enhanced"
    if not ed.is_dir():
        return None
    for side in ed.glob("*.json"):
        try:
            rec = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("preset") != preset or rec.get("playbackSource") != "ENHANCED":
            continue
        p = Path(str(rec.get("playbackPath") or ""))
        if p.is_file():
            return p
    return None


def short_cmd(video_id: str) -> list:
    """Composite a downloaded YouTube Short → hevc_qsv, preserving the source
    aspect ratio (vertical Shorts get pillarboxed, never stretched; distorted
    enhanced derivatives are un-stretched first). Czech subtitles are burned
    only when a non-empty SRT exists. Output is normalized to exactly 60 fps
    CFR so VFR sources cannot inject pacing jitter into the 60 Hz path."""
    vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
    orig = vd / "source.mp4"
    src = orig
    with _STATE_LOCK:
        enh = (_STATE.get("enhancement") or {}).get("playbackPath")
    if enh and Path(enh).is_file() and Path(enh).resolve() != src.resolve():
        src = Path(enh)
    srt = vd / "cs_subtitles.srt"
    if not src.is_file():
        raise RuntimeError(f"source.mp4 missing for {video_id}")
    # AR-preserving fit + pillarbox; force_divisible_by=2 keeps p010le legal.
    fit_pad = (
        "scale=3840:2160:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2:flags=lanczos,"
        "pad=3840:2160:(ow-iw)/2:(oh-ih)/2:color=black,"
        "fps=60,format=p010le"
    )
    # Undo the engine's square squash for portrait sources (AR bug).
    ar_scale = None
    if src.resolve() != orig.resolve() and orig.is_file():
        om = _probe_video_dims(orig)
        if om:
            ar_scale = _correct_ar_scale(src, om[0], om[1])
    # An empty transcript (e.g. a clip with no detectable speech) yields a
    # 0-byte SRT; ffmpeg's subtitles filter refuses to open it, so only burn
    # subtitles when the file is present and non-empty.
    if srt.is_file() and srt.stat().st_size > 0:
        dims = _probe_video_dims(src)
        if dims and ar_scale:
            sw, sh = ar_scale[1], ar_scale[2]  # corrected canvas
        elif dims:
            sw, sh = dims
        else:
            sw, sh = 3840, 2160
        font = max(28, round(110 * sw / 3840))
        margin = max(24, round(170 * sw / 3840))
        srt_path = str(srt).replace("\\", "/").replace(":", "\\:")
        subs = (
            f"subtitles='{srt_path}':force_style='PlayResX={sw},PlayResY={sh},"
            f"FontSize={font},MarginV={margin},PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00101010,BorderStyle=1,Outline=3,Shadow=0,Alignment=2',"
        )
        vf = ((ar_scale[0] + ",") if ar_scale else "") + subs + fit_pad
    else:
        vf = ((ar_scale[0] + ",") if ar_scale else "") + fit_pad
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1",
        "-i", str(src),
        "-vf", vf,
        "-map", "0:v:0",
        "-c:v", "hevc_qsv", "-profile:v", "main10", "-preset", "veryfast",
        "-bf", "0", "-g", "30", "-forced_idr", "1", "-async_depth", "2",
        "-aud", "1", "-adaptive_i", "0", "-scenario", "livestreaming",
        "-b:v", "18M", "-maxrate", "28M", "-bufsize", "40M",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-color_range", "tv",
        "-f", "hevc", "pipe:1",
    ]


def _render_compare(video_id: str, right_path: Path, out_name: str, label: str,
                    hdr: bool = False, left_path: Path | None = None,
                    left_label: str = "ORIGINAL") -> Path | None:
    """Offline-render the split comparison (left / processed right)
    to enhanced/<out_name>.mp4 (3840x2160 HEVC Main10 10-bit CFR 60).
    left_path defaults to source.mp4 (labeled ORIGINAL); the cinematic ladder
    passes the previous step's output so each A/B isolates that step's
    contribution (e.g. CLEANUP vs DETAIL + CAS). With hdr=True the composite
    is signaled BT.2020/PQ + HDR10 SEI so the TV switches to HDR mode — an SDR
    left half then reads as 'SDR on an HDR display', which is the honest
    comparison. The live stream -c copy's this file, so playback costs ~zero
    live CPU."""
    vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
    out = vd / "enhanced" / f"{out_name}.mp4"
    if out.is_file() and out.stat().st_size > 1000:
        return out
    orig = left_path or (vd / "source.mp4")
    if not orig.is_file() or not right_path.is_file():
        return None
    pad_half = "pad=1920:2160:(ow-iw)/2:(oh-ih)/2:color=black"
    om = _probe_video_dims(orig)
    cw = 0
    if om and om[0] > 0 and om[1] > 0:
        cw = int(round(2160 * om[0] / om[1]))
        cw -= cw % 2
    if 2 <= cw <= 1920:
        half = f"scale={cw}:2160:flags=bicubic,{pad_half}"
    else:
        half = ("scale=1920:2160:force_original_aspect_ratio=decrease:"
                "force_divisible_by=2:flags=bicubic," + pad_half)
    fc = (f"[0:v]{half}[L];[1:v]{half}[R];"
          "[L][R]hstack=inputs=2,"
          "drawbox=x=1919:y=0:w=2:h=2160:color=white@0.85:t=fill")
    font = Path(r"C:\Windows\Fonts\arialbd.ttf")
    if font.is_file():
        fp = str(font).replace("\\", "/").replace(":", "\\:")
        fc += (f",drawtext=fontfile='{fp}':text='{left_label}':x=48:y=40:fontsize=76:"
               "fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=16"
               f",drawtext=fontfile='{fp}':text='{label}':x=1992:y=40:fontsize=76:"
               "fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=16")
    fc += ",fps=60,format=p010le[v]"
    tmp = out.with_suffix(".tmp.mp4")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-i", str(orig), "-i", str(right_path),
        "-filter_complex", fc, "-map", "[v]",
        "-c:v", "hevc_qsv", "-profile:v", "main10", "-preset", "veryfast",
        "-bf", "0", "-g", "30", "-forced_idr", "1", "-async_depth", "2",
        "-aud", "1", "-adaptive_i", "0", "-scenario", "livestreaming",
        "-b:v", "18M", "-maxrate", "28M", "-bufsize", "40M",
        "-color_range", "tv",
    ]
    if hdr:
        cmd += ["-color_primaries", "bt2020", "-color_trc", "smpte2084",
                "-colorspace", "bt2020nc"]
    else:
        cmd += ["-color_primaries", "bt709", "-color_trc", "bt709",
                "-colorspace", "bt709"]
    cmd += ["-movflags", "+faststart", str(tmp)]
    try:
        import subprocess as _sp
        r = _sp.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1000:
            print(f"CMP_RENDER_FAIL {out_name} rc={r.returncode} "
                  f"{(r.stderr or '')[-600:]}", flush=True)
            try:
                tmp.unlink()
            except Exception:
                pass
            return None
        # ffmpeg's hevc_metadata bsf here cannot write MaxCLL/master-display
        # SEI, so for HDR steps tag the composite with a QSVEnc pass that
        # embeds proper HDR10 SEI (same signaling the native HDR path uses).
        if hdr:
            qsenc = (Path(__file__).resolve().parents[1] / "qsvenc" / "QSVEncC64.exe")
            if qsenc.is_file():
                sei_tmp = out.with_suffix(".sei.mp4")
                qsv = [str(qsenc), "-i", str(tmp), "--avsw",
                       "-c", "hevc", "--profile", "main10",
                       "--output-depth", "10", "--output-csp", "yuv420",
                       "--colormatrix", "bt2020nc", "--colorprim", "bt2020",
                       "--transfer", "smpte2084",
                       "--max-cll", "1000,400",
                       "--master-display",
                       "G(13250,34500)B(7500,3000)R(34000,16000)"
                       "WP(15635,16450)L(10000000,1)",
                       "-o", str(sei_tmp)]
                r2 = _sp.run(qsv, capture_output=True, text=True, timeout=1800)
                if r2.returncode == 0 and sei_tmp.is_file() and sei_tmp.stat().st_size > 1000:
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                    tmp = sei_tmp
                else:
                    print(f"CMP_HDR_SEI_FAIL {out_name} rc={r2.returncode} "
                          f"{(r2.stderr or '')[-300:]}", flush=True)
        tmp.replace(out)
        print(f"CMP_RENDER_OK {video_id} {out_name} {out.stat().st_size}", flush=True)
        return out
    except Exception as e:
        print(f"CMP_RENDER_ERR {out_name} {e}", flush=True)
        return None


def _render_ab_file(video_id: str, preset: str) -> Path | None:
    """A/B for an IntelVideoEngine derivative (SDR signaling)."""
    der = _derivative_for(video_id, preset)
    if der is None:
        return None
    return _render_compare(video_id, der, f"ab_{preset}",
                           f"ENHANCED {preset}", hdr=False)


def ab_stream_cmd(video_id: str, preset: str) -> list:
    """Stream a pre-rendered A/B composite with zero re-encode (-c copy)."""
    vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
    f = vd / "enhanced" / f"ab_{preset}.mp4"
    if not f.is_file():
        raise RuntimeError(f"ab render missing for {video_id}:{preset}")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1",
        "-i", str(f),
        "-c", "copy",
        "-f", "hevc", "pipe:1",
    ]


def cin_stream_cmd(video_id: str, step: str) -> list:
    """Stream a pre-rendered cinematic comparison with zero re-encode."""
    vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
    f = vd / "enhanced" / f"ab_{step}.mp4"
    if not f.is_file():
        raise RuntimeError(f"cin render missing for {video_id}:{step}")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1",
        "-i", str(f),
        "-c", "copy",
        "-f", "hevc", "pipe:1",
    ]


def showroom_stream_cmd(video_id: str, name: str) -> list:
    """Stream a pre-rendered SHOWROOM asset (compare or A/B/C) with zero
    re-encode. Names: compare, A, B, C -> enhanced/showroom_<name>.mp4."""
    vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
    f = vd / "enhanced" / f"showroom_{name.lower()}.mp4"
    if not f.is_file():
        raise RuntimeError(f"showroom asset missing for {video_id}:{name}")
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1",
        "-i", str(f),
        "-c", "copy",
        "-f", "hevc", "pipe:1",
    ]


def short_audio_cmd(video_id: str) -> list:
    """Decode original audio to PCM S16LE 48 kHz stereo, realtime.

    Audio is taken from the same effective media path as the video (the
    enhanced derivative when one is active) — the derivative carries the
    preserved source audio, so both paths are the original speech.
    """
    vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
    src = vd / "source.mp4"
    with _STATE_LOCK:
        enh = (_STATE.get("enhancement") or {}).get("playbackPath")
    if enh and Path(enh).is_file() and Path(enh).resolve() != src.resolve():
        src = Path(enh)
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re", "-stream_loop", "-1",
        "-i", str(src),
        "-vn",
        "-ac", str(AUDIO_CH),
        "-ar", str(AUDIO_RATE),
        "-c:a", "pcm_s16le",
        "-f", "s16le",
        "pipe:1",
    ]


def desktop_cmd() -> list:
    """Windows DXGI Desktop Duplication capture (dxcam) → hevc_qsv.

    GPU-native frame acquisition replaces gdigrab. The desktop is captured
    at its TRUE resolution (dxcam reports it at build time); if that is not
    3840×2160 the frame is letterboxed into the decoder canvas — never
    stretched and never re-tagged. SDR stays SDR (bt709), never PQ.
    """
    try:
        import dxcam as _dxcam
        info = str(_dxcam.output_info())
        import re as _re
        m = _re.search(r"Res:\((\d+),\s*(\d+)\)", info)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    except Exception:
        w = h = 0
    if w <= 0 or h <= 0:
        # honest fallback: encoder canvas size, never claimed as captured
        w, h = 3840, 2160
    if (w, h) == (3840, 2160):
        vf = "format=p010le"
    else:
        vf = ("scale=3840:2160:force_original_aspect_ratio=decrease,"
              "pad=3840:2160:(ow-iw)/2:(oh-ih)/2,format=p010le")
    print(f"DESKTOP_CAPTURE {w}x{h} source truth", flush=True)
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", "30",
        "-i", "pipe:0",
        "-vf", vf,
        "-c:v", "hevc_qsv", "-profile:v", "main10", "-preset", "veryfast",
        "-bf", "0", "-g", "30", "-forced_idr", "1", "-async_depth", "2",
        "-aud", "1", "-adaptive_i", "0", "-scenario", "livestreaming",
        "-b:v", "18M", "-maxrate", "28M", "-bufsize", "40M",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-colorspace", "bt709", "-color_range", "tv",
        "-f", "hevc", "pipe:1",
    ]


def native_cmd() -> list:
    """Native zero-copy GPU engine: D3D11 scene render -> P010 -> QSV HEVC Main10.

    Replaces the Python renderer + ffmpeg hevc_qsv for the scene source. The
    engine runs in pipe mode (no --out): reads framed state JSON on stdin and
    writes [u32 BE len][kind][payload] on stdout (kind 1 = Annex-B AU).

    The session FPS is the single source of truth (P1.1): passed as --fps and
    --gop (1 s IDR interval, so GOP == fps for both 30 and 60), plus --fps-den
    for the fractional display-rate case (59.94 = 60000/1001). The engine
    resolves its own display-profile flip default (vertical for the box).
    """
    exe = Path(__file__).resolve().parent / "native_engine" / "target" / "release" / "toastovac_gpu.exe"
    if not exe.exists():
        raise FileNotFoundError(f"native engine not built: {exe}")
    # P5: the session rate is a rational (num, den) — never approximated.
    # 60000/1001 stays 60000/1001; 60/1 stays 60/1. The engine receives the
    # exact numerator/denominator pair (--fps num --fps-den den -> oneVPL
    # FrameRateExtN/D) and paces its clock from the same rational.
    num, den = session_fraction()
    fps = num / den
    gop = max(1, round(fps))
    return [str(exe), "--fps", str(num), "--fps-den", str(den), "--gop", str(gop), "--pace"]


def golden_cmd(bsf: bool = False) -> list:
    """Stream the golden HDR10 clip's raw HEVC (60 fps, stream copy) through
    the same TZHL pipeline the native engine uses. Isolates bitstream-vs-path:
    golden content through the box's raw MediaCodec decoder answers whether the
    180° flip is the QSV bitstream or the decode/display path. bsf=True applies
    the same hevc_metadata rewrite used on the native path, to test whether the
    bsf-modified SPS is what the Amlogic decoder dislikes."""
    src = Path(__file__).resolve().parent / "hdr10_golden.mp4"
    if not src.exists():
        raise FileNotFoundError(f"golden clip missing: {src}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",
           "-stream_loop", "-1", "-i", str(src), "-c", "copy"]
    if bsf:
        cmd += ["-bsf:v", BSF_OPTS]
    cmd += ["-f", "hevc", "pipe:1"]
    return cmd


def golden_bsf_cmd() -> list:
    return golden_cmd(bsf=True)


def golden_qsv_cmd() -> list:
    """Re-encode the golden clip with the SAME Meteor Lake QSV encoder (via
    ffmpeg hevc_qsv, default settings) and stream it. If QSV output flips on
    the box regardless of content, the flip is encoder-wide; if it stays
    correct, the native engine's specific encode settings trigger it.
    slices=True mirrors the engine's NumSlice=2 to test the slice hypothesis.
    GOLDEN_QSV_SRC overrides the input file (any raw HEVC/MP4)."""
    src = Path(os.environ.get("GOLDEN_QSV_SRC", "")) if os.environ.get("GOLDEN_QSV_SRC") else \
        Path(__file__).resolve().parent / "hdr10_golden.mp4"
    if not src.exists():
        raise FileNotFoundError(f"golden clip missing: {src}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-re",
           "-stream_loop", "-1", "-i", str(src),
           "-c:v", "hevc_qsv", "-profile:v", "main10", "-preset", "veryfast",
           "-b:v", "18M", "-maxrate:v", "28M", "-bufsize:v", "5M",
           "-g", "30", "-bf", "0"]
    if os.environ.get("GOLDEN_QSV_SLICES", "0") == "1":
        cmd += ["-slices", "2"]
    cmd += ["-f", "hevc", "pipe:1"]
    return cmd


DESKTOP_STATS = {
    "capture_backend": "dxgi_desktop_duplication",
    "capture_fps": 0.0,
    "capture_latency_ms": 0.0,
    "dropped_frames": 0,
    "queue_depth": 0,
    "frames_written": 0,
    "started": 0.0,
    "error": "",
}


def pump_dxgi(proc: subprocess.Popen, stop: threading.Event, fps_target: int = 30):
    """DXGI capture loop → raw rgb24 frames into the QSV encoder stdin.

    The capture thread runs faster than the delivery pace (dxcam target 60)
    so a fresh frame is always available; the pump itself paces delivery at
    exactly ``fps_target`` so the encoder and the box see a stable rate.
    """
    try:
        import dxcam
    except Exception as e:
        DESKTOP_STATS["error"] = f"dxcam import failed: {e}"
        raise
    try:
        cam = dxcam.create(output_idx=0, output_color="RGB")
        cam.start(target_fps=60, video_mode=True)
    except Exception as e:
        DESKTOP_STATS["error"] = f"dxcam create failed: {e}"
        raise
    DESKTOP_STATS["started"] = time.time()
    t0 = time.perf_counter()
    n = 0
    skipped = 0
    lat_sum = 0.0
    try:
        while not stop.is_set():
            target_t = t0 + n / fps_target
            now = time.perf_counter()
            if now < target_t:
                time.sleep(target_t - now)
            g0 = time.perf_counter()
            frame = cam.get_latest_frame()
            if frame is None:
                skipped += 1
                time.sleep(0.002)
                continue
            lat_sum += (time.perf_counter() - g0) * 1000.0
            proc.stdin.write(frame.tobytes())
            n += 1
            if n % 30 == 0:
                el = max(0.001, time.perf_counter() - t0)
                DESKTOP_STATS.update({
                    "capture_fps": round(n / el, 2),
                    "capture_latency_ms": round(lat_sum / n, 2),
                    "dropped_frames": skipped,
                    "frames_written": n,
                    "queue_depth": _Q.qsize(),
                })
    except (BrokenPipeError, ValueError):
        pass
    finally:
        try:
            cam.stop()
            cam.release()
        except Exception:
            pass
        try:
            proc.stdin.close()
        except Exception:
            pass
        el = max(0.001, time.perf_counter() - t0)
        DESKTOP_STATS.update({
            "capture_fps": round(n / el, 2),
            "frames_written": n,
            "dropped_frames": skipped,
        })
        print(f"DESKTOP_DXGI_END frames={n} skipped={skipped}", flush=True)


def pump_ffmpeg(proc: subprocess.Popen, label: str):
    """Pump encoder stdout Annex-B chunks into the shared queue."""
    try:
        while True:
            chunk = proc.stdout.read(64 * 1024)
            if not chunk:
                break
            _Q.put(("chunk", chunk))
    except Exception:
        pass
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        _Q.put(("eos",))
        with _STATE_LOCK:
            _STATE["last_error"] = f"{label} ended rc={proc.poll()}"
        print(f"PRODUCER_END {label} rc={proc.poll()}", flush=True)


# HEVC SPS VUI rewrite applied by the persistent bsf on the native path:
# BT.2020 primaries (9) / PQ transfer (16) / BT.2020nc matrix (9), limited
# range, video_format=5 (unspecified — matches the golden HDR10 clip) and
# level 5.1 (153, golden also 5.1). This is the VUI source for HDR signaling
# (hdrVuiSource=HEVC_METADATA_BSF).
#
# P1.3: HDR static metadata. The mfx-gen runtime ignores the Init ext buffers
# mfxExtMasteringDisplayColourVolume / mfxExtContentLightLevelInfo too — the
# engine's own output carries only buffering-period + pic-timing SEI (verified
# via trace_headers; the box layer reports hdr metadata types=0 vs golden=3).
# The same persistent bsf therefore also inserts the mastering-display + CLL
# SEI, preserving the existing proven policy: MaxCLL=1000, MaxFALL=400 and the
# BT.2020 mastering primaries the encoder config already declares
# (hdrSeiSource=HEVC_METADATA_BSF). Units for master-display are 0.00002
# (x/y) and 0.0001 cd/m² (L); values match the golden clip's HDR10 metadata.
BSF_OPTS = (
    "hevc_metadata=colour_primaries=9:transfer_characteristics=16:"
    "matrix_coefficients=9:video_full_range_flag=0:video_format=5:level=153"
)


# ── HDR10 static metadata SEI injection (P1.3) ───────────────────────────
# The mfx-gen runtime ignores the Init mfxExtMasteringDisplayColourVolume /
# mfxExtContentLightLevelInfo buffers too (same Init-ext-buffer drop as the
# VUI colour description — verified via trace_headers: the engine's own SEI
# carries only buffering-period + pic-timing; the box layer reports
# hdr metadata types=0 vs golden=3). The persistent bsf cannot add SEI on
# this ffmpeg build (no max_cll/master-display options), so the HDR10 SEI is
# injected here on IDR AUs — bitstream-only CPU work, byte layout matched to
# the golden clip (payload 144 then 137; G,B,R primaries order; MaxCLL=1000,
# MaxFALL=400; BT.2020 mastering primaries as declared in qsv.rs).
# hdrSeiSource=HEVC_METADATA_BSF. Golden ground truth:
#   MaxCLL=1000 MaxFALL=400, WP=(15635,16450), L=(10000000,5)
HDR10_MDCV = struct.pack(
    ">8HII",
    8500, 39850,      # G x,y  (0.170, 0.797)
    6550, 2300,       # B x,y  (0.131, 0.046)
    35400, 14600,     # R x,y  (0.708, 0.292)
    15635, 16450,     # white point D65
    10000000, 50,     # max 1000 cd/m², min 0.005 cd/m² (×0.0001)
)
HDR10_CLL = struct.pack(">HH", 1000, 400)  # MaxCLL, MaxFALL


def _hdr10_sei_nal() -> bytes:
    """Annex-B prefix-SEI NALs (type 39): content-light (144) then mastering-
    display (137) as two separate NALs, byte layout matched to the golden
    HDR10 clip (which the Amlogic decoder demonstrably accepts: the box layer
    reported hdr metadata types=3 for golden vs 0 for the engine stream).
    Emulation prevention is applied to the payloads."""
    cll_payload = bytes((144, 4)) + HDR10_CLL + b"\x80"
    mdcv_payload = bytes((137, 24)) + HDR10_MDCV + b"\x80"

    def esc(payload: bytes) -> bytes:
        out = bytearray()
        zeros = 0
        for b in payload:
            if zeros >= 2 and b <= 3:
                out.append(3)
                zeros = 0
            out.append(b)
            zeros = zeros + 1 if b == 0 else 0
        return bytes(out)

    return (b"\x00\x00\x00\x01\x4e\x01" + esc(cll_payload)
            + b"\x00\x00\x00\x01\x4e\x01" + esc(mdcv_payload))


def _inject_hdr10_sei(au: bytes) -> bytes:
    """Insert the HDR10 SEI into every VCL AU right AFTER the leading
    VPS/SPS/PPS NALs (golden layout: config, then SEI, then slice). Keeping
    the config at the AU start lets the hevc demuxer sync immediately — when
    the SEI preceded the config, the -probesize window missed the SPS and the
    bsf buffered ~0.4 s at 60 fps. Config AUs pass through untouched; the mux
    routes the config NALs to the config packet and keeps the SEI ahead of
    the slice in the frame AU."""
    if not (_scan_nal_types(au) & VCL):
        return au
    i = 0
    n = len(au)
    split = None
    while i < n - 3:
        if au[i] == 0 and au[i + 1] == 0:
            if i + 3 < n and au[i + 2] == 0 and au[i + 3] == 1:
                sc = 4
            elif i + 2 < n and au[i + 2] == 1:
                sc = 3
            else:
                i += 1
                continue
            t = (au[i + sc] >> 1) & 0x3F
            if t not in (32, 33, 34):  # first non-config NAL
                split = i
                break
            i += sc
            continue
        i += 1
    sei = _hdr10_sei_nal()
    if split is None:
        return sei + au
    return au[:split] + sei + au[split:]


def _scan_nal_types(au: bytes) -> set:
    """Return the set of HEVC NAL unit types (bits 6..1 of the header byte)
    present in an Annex-B AU, for 3- and 4-byte start codes."""
    types = set()
    i = 0
    n = len(au)
    while i < n - 4:
        if au[i] == 0 and au[i + 1] == 0:
            if au[i + 2] == 0 and au[i + 3] == 1:
                types.add((au[i + 4] >> 1) & 0x3F)
                i += 4
                continue
            if au[i + 2] == 1:
                types.add((au[i + 3] >> 1) & 0x3F)
                i += 3
                continue
        i += 1
    return types


# BSF instrumentation (P1.2): input AU count, output chunk count, per-AU
# latency approximation (feed timestamp -> first following chunk), queue depth.
BSF_STATS = {
    "input_aus": 0,
    "output_chunks": 0,
    "latency_ms": [],  # rolling samples (newest-AU transit), p50/p95 on status read
    "queue_depth": 0,
    "backlog": 0,      # pending AU feed timestamps not yet emitted as a chunk
    "enabled": True,
}
_BSF_FEED_TS = collections.deque(maxlen=256)


def _bsf_p50_p95(samples) -> tuple:
    if not samples:
        return 0.0, 0.0
    v = sorted(samples)
    n = len(v)
    return v[int(n * 0.5)], v[min(n - 1, int(n * 0.95))]


def pump_native(proc: subprocess.Popen, label: str):
    """Pump the native engine's framed stdout ([u32 BE len][kind][payload]).

    kind 1 = complete Annex-B AU -> optional HEVC VUI rewrite through a
    persistent ffmpeg hevc_metadata bsf (TOASTOVAC_BSF=0 disables), then the
    shared queue. The muxer re-derives config/IDR flags from the byte stream,
    so the bsf only needs to emit Annex-B. kind 2 = metrics (captured into
    _STATE["native_metrics"]), kind 3 = event (logged; DISPLAY_PROFILE is
    captured into _STATE["orientation"]).
    """
    import struct

    def read_exact(n: int):
        buf = b""
        while len(buf) < n:
            chunk = proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    bsf = os.environ.get("TOASTOVAC_BSF", "1") != "0"
    BSF_STATS["enabled"] = bsf
    ff: subprocess.Popen | None = None
    ff_pending: list = []  # AUs seen before the first config AU (dropped)
    ff_started = False

    def ff_spawn():
        nonlocal ff
        # Persistent process (not per-frame spawn): rewrites the SPS VUI with
        # BT.2020 / PQ / BT.2020nc colour description on stream copy. The
        # encoder runtime drops mfxExtVideoSignalInfo, so this is the VUI
        # source (hdrVuiSource=HEVC_METADATA_BSF).
        # nobuffer + low_delay: without these the hevc demuxer buffers ~30 AUs
        # (~1 s at 30 fps) before emitting — measured via bsfLatencyP50.
        ff = subprocess.Popen(
            ["ffmpeg", "-v", "error",
             "-fflags", "nobuffer", "-flags", "low_delay",
             "-probesize", "32", "-analyzeduration", "0",
             "-f", "hevc", "-i", "pipe:0",
             "-c", "copy", "-bsf:v", BSF_OPTS, "-f", "hevc", "pipe:1",
             "-flush_packets", "1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)

        def reader():
            try:
                while True:
                    chunk = ff.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    BSF_STATS["output_chunks"] += 1
                    # Latency sample = chunk arrival - NEWEST feed ts. ffmpeg
                    # coalesces several AUs into one pipe read, so popping the
                    # oldest ts would report the whole coalesced batch's age
                    # (a measurement artifact, not real buffering — verified:
                    # bsfOut chunks >= bsfIn AUs, so AUs are not held back).
                    # The deque length is the true pending-AU backlog.
                    if _BSF_FEED_TS:
                        ts = _BSF_FEED_TS.pop()
                        lat = (time.perf_counter() - ts) * 1000.0
                        BSF_STATS["latency_ms"].append(lat)
                        if len(BSF_STATS["latency_ms"]) > 1200:
                            del BSF_STATS["latency_ms"][:600]
                    BSF_STATS["backlog"] = len(_BSF_FEED_TS)
                    _Q.put(("chunk", chunk))
            except Exception:
                pass
            finally:
                try:
                    ff.stdout.close()
                except Exception:
                    pass
                _Q.put(("eos",))
                with _STATE_LOCK:
                    _STATE["last_error"] = f"{label} bsf ended rc={ff.poll()}"
                print(f"PRODUCER_END {label} bsf rc={ff.poll()}", flush=True)

        threading.Thread(target=reader, daemon=True).start()

    def ff_feed(data: bytes):
        if ff is None or ff.stdin is None:
            return
        BSF_STATS["input_aus"] += 1
        BSF_STATS["queue_depth"] = _Q.qsize()
        _BSF_FEED_TS.append(time.perf_counter())
        try:
            ff.stdin.write(data)
            ff.stdin.flush()
        except Exception:
            pass

    try:
        while True:
            hdr = read_exact(5)
            if hdr is None:
                break
            (ln,) = struct.unpack(">I", hdr[:4])
            kind = hdr[4]
            if ln > (64 << 20):  # sanity cap
                continue
            body = read_exact(ln)
            if body is None:
                break
            if kind == 1 and body:
                body = _inject_hdr10_sei(body)
                if bsf:
                    if not ff_started:
                        if _scan_nal_types(body) & {32, 33, 34}:  # VPS/SPS/PPS
                            ff_started = True
                            ff_spawn()
                            for p in ff_pending:
                                ff_feed(p)
                            ff_pending.clear()
                            ff_feed(body)
                        else:
                            if len(ff_pending) < 300:
                                ff_pending.append(body)
                    else:
                        ff_feed(body)
                else:
                    _Q.put(("chunk", body))
            elif kind == 2:
                try:
                    m = json.loads(body.decode("utf-8", "replace"))
                    if isinstance(m, dict):
                        with _STATE_LOCK:
                            _STATE["native_metrics"] = m
                except Exception:
                    pass
            elif kind == 3:
                try:
                    text = body.decode("utf-8", "replace")[:400]
                    print("NATIVE_EVENT", text, flush=True)
                    if '"DISPLAY_PROFILE"' in text or "DISPLAY_PROFILE" in text:
                        try:
                            ev = json.loads(body.decode("utf-8", "replace"))
                            with _STATE_LOCK:
                                _STATE["orientation"] = (ev.get("data") or {})
                        except Exception:
                            pass
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        if ff is not None:
            try:
                ff.stdin.close()
            except Exception:
                pass
        if not bsf or ff is None:
            _Q.put(("eos",))
            with _STATE_LOCK:
                _STATE["last_error"] = f"{label} ended rc={proc.poll()}"
            print(f"PRODUCER_END {label} rc={proc.poll()}", flush=True)


def native_state_loop(proc: subprocess.Popen, stop: threading.Event):
    """Feed framed state JSON to the native engine's stdin at current_fps().

    Mirrors scene_render_loop's HUD (live frame counter / fps / PQ), so the
    engine renders the same live clock + HUD it would in standalone file mode.
    The HUD fps is sent as an integer (the engine parses it as u64); the real
    fractional rate lives in the engine's own frame interval (--fps/--fps-den).
    """
    import struct
    fps = current_fps()
    hud_fps = round(fps)
    # P5.1: pace the state feed from the exact rational rate (n * den / num),
    # not an approximate float — the engine's scene clock derives from the
    # same numerator/denominator the encoder and pacing clock use.
    num, den = session_fraction()
    t0 = time.perf_counter()
    n = 0
    while not stop.is_set():
        target = t0 + n * den / num
        now = time.perf_counter()
        if now < target:
            time.sleep(target - now)
        msg = json.dumps({"t": n * den / num, "hud": {"live": n, "fps": hud_fps, "pq": "1"}},
                         separators=(",", ":")).encode("utf-8")
        try:
            proc.stdin.write(struct.pack(">I", len(msg)))
            proc.stdin.write(msg)
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            break
        n += 1
        STATS["frames"] = n
    try:
        proc.stdin.close()
    except Exception:
        pass


def pump_pcm(fh, label: str):
    """Pump PCM S16LE 48 kHz stereo into the mux as timed AUDIO_FRAME packets.

    The audio subprocess pipe is opened unbuffered (raw) so ``read()`` can
    return partial chunks (typically ~4 KB on Windows) — without assembly the
    mux would emit ~2× the packets carrying the same 1× byte rate. Read until
    a full PCM_CHUNK (40 ms) is assembled so every packet is exactly one
    40 ms frame (25 packets/s at 1×).
    """
    pts0 = time.monotonic()
    sent = 0
    buf = b""
    try:
        while True:
            if not buf:
                chunk = fh.read(PCM_CHUNK)
                if not chunk:
                    break
                buf = chunk
            while len(buf) >= PCM_CHUNK:
                pts_us = int(sent * 1_000_000 / AUDIO_BYTES_PER_SEC)
                _Q.put(("audio", pts_us, buf[:PCM_CHUNK]))
                sent += PCM_CHUNK
                buf = buf[PCM_CHUNK:]
            more = fh.read(PCM_CHUNK)
            if not more:
                break
            buf += more
    except Exception as e:
        print("PCM_FAIL", label, e, flush=True)
    finally:
        try:
            fh.close()
        except Exception:
            pass
        print("PCM_END", label, "bytes", sent, "wall",
              f"{time.monotonic() - pts0:.1f}s", flush=True)


def scene_render_loop(proc: subprocess.Popen, stop: threading.Event):
    from gpu_scene import create_scene  # GPU SceneGraph renderer w/ CPU fallback
    scene = create_scene()
    fps = current_fps()
    t0 = time.perf_counter()
    n = 0
    while not stop.is_set():
        target = t0 + n / fps
        now = time.perf_counter()
        if now < target:
            time.sleep(target - now)
        try:
            blob = scene.render(n / fps, {"live": n, "fps": fps, "pq": "1"}).tobytes()
            proc.stdin.write(blob)
        except (BrokenPipeError, ValueError):
            break
        n += 1
        STATS["frames"] = n
    try:
        proc.stdin.close()
    except Exception:
        pass


class Producer:
    """Owns one source's ffmpeg process (or renderer+encoder) and pumps to _Q."""

    def __init__(self, source: str, cmd: list, audio_cmd: list | None = None):
        self.source = source
        self.cmd = cmd
        self.audio_cmd = audio_cmd
        self.proc: subprocess.Popen | None = None
        self.audio_proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0)
        with _STATE_LOCK:
            _STATE["producer_pid"] = self.proc.pid
        if self.source == "native":
            pump_fn = pump_native
        else:
            pump_fn = pump_ffmpeg
        pump = threading.Thread(target=pump_fn, args=(self.proc, self.source), daemon=True)
        pump.start()
        self.threads.append(pump)
        stderr = threading.Thread(target=self._stderr_pump, daemon=True)
        stderr.start()
        self.threads.append(stderr)
        if self.audio_cmd:
            self.audio_proc = subprocess.Popen(
                self.audio_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0)
            pcm = threading.Thread(
                target=pump_pcm, args=(self.audio_proc.stdout, self.source),
                daemon=True)
            pcm.start()
            self.threads.append(pcm)
        if self.source == "scene":
            render = threading.Thread(
                target=scene_render_loop, args=(self.proc, self._stop), daemon=True)
            render.start()
            self.threads.append(render)
        elif self.source == "native":
            st = threading.Thread(
                target=native_state_loop, args=(self.proc, self._stop), daemon=True)
            st.start()
            self.threads.append(st)
        elif self.source == "interview":
            from interview_scene import render_loop_into
            render = threading.Thread(
                target=render_loop_into, args=(self.proc, self._stop), daemon=True)
            render.start()
            self.threads.append(render)
        elif self.source == "desktop":
            dxgi = threading.Thread(
                target=pump_dxgi, args=(self.proc, self._stop), daemon=True)
            dxgi.start()
            self.threads.append(dxgi)

    def _stderr_pump(self) -> None:
        if self.proc is None:
            return
        for line in iter(self.proc.stderr.readline, b""):
            sys.stderr.buffer.write(b"FF-" + self.source.encode() + b" " + line)
            sys.stderr.buffer.flush()

    def stop(self) -> None:
        self._stop.set()
        for p in (self.proc, self.audio_proc):
            if p is None:
                continue
            try:
                p.kill()
            except Exception:
                pass
            try:
                p.wait(timeout=3)
            except Exception:
                pass
        time.sleep(0.2)


def switch_source(source: str, detail: str = "") -> bool:
    """Stop the active producer, start the new one, reset the AU assembler.

    For the native source the resolved session FPS is part of the identity:
    a cast.scene with a different fps must restart the engine (its --fps,
    --gop and the state-feed pacing all depend on it), so the same-source
    early return only applies when the producer's fps matches the current
    session fps."""
    global _Q
    with _STATE_LOCK:
        old = _STATE["producer"]
        same_fps = source != "native" or _STATE.get("producer_fps") == current_fps()
        same_detail = source not in ("ab", "cin") or _STATE.get("detail") == detail
        if old is not None and old.source == source and same_detail and same_fps \
                and old.proc and old.proc.poll() is None:
            _STATE["detail"] = detail
            return True
    if old is not None:
        old.stop()
    cmd_builder = {"scene": scene_cmd, "native": native_cmd, "short": None, "desktop": desktop_cmd, "golden": golden_cmd, "goldenbsf": golden_bsf_cmd, "goldenqsv": golden_qsv_cmd}.get(source)
    audio_cmd = None
    if source == "short":
        cmd = short_cmd(detail) if detail else None
        if cmd is None:
            with _STATE_LOCK:
                _STATE["last_error"] = "cast.youtube: no video_id"
            return False
        audio_cmd = short_audio_cmd(detail)
    elif source == "ab":
        vid, _, preset = (detail or "").partition(":")
        cmd = ab_stream_cmd(vid, preset or "fast") if vid else None
        if cmd is None:
            with _STATE_LOCK:
                _STATE["last_error"] = "cast.ab: need video_id:preset"
            return False
        audio_cmd = short_audio_cmd(vid)
    elif source == "cin":
        vid, _, step = (detail or "").partition(":")
        cmd = cin_stream_cmd(vid, step) if vid else None
        if cmd is None:
            with _STATE_LOCK:
                _STATE["last_error"] = "cast.cin: need video_id:step"
            return False
        audio_cmd = short_audio_cmd(vid)
    elif source == "showroom":
        vid, _, name = (detail or "").partition(":")
        cmd = showroom_stream_cmd(vid, name) if vid else None
        if cmd is None:
            with _STATE_LOCK:
                _STATE["last_error"] = "cast.showroom: need video_id:name"
            return False
        audio_cmd = short_audio_cmd(vid)
    elif source == "interview":
        from interview_scene import interview_cmd
        cmd = interview_cmd()
    else:
        cmd = cmd_builder() if cmd_builder else None
    try:
        prod = Producer(source, cmd, audio_cmd=audio_cmd)
        prod.start()
    except Exception as e:
        with _STATE_LOCK:
            _STATE["last_error"] = f"{source} start failed: {e}"
        print("SOURCE_FAIL", source, e, flush=True)
        return False
    with _STATE_LOCK:
        _STATE["producer"] = prod
        _STATE["source"] = source
        _STATE["detail"] = detail
        _STATE["started"] = time.time()
        _STATE["last_error"] = ""
        if source == "native":
            _STATE["producer_fps"] = current_fps()
    _Q.put(("reset", None))
    print(f"SOURCE_SWITCH {source} {detail} fps={current_fps()}", flush=True)
    return True


# ── AU assembler + TZHL mux ─────────────────────────────────────────────
def mux_loop(clients: list, lock: threading.Lock, stop: threading.Event):
    assembler = _NalAssembler()
    config = bytearray()
    au = bytearray()
    au_has_vcl = False
    au_irap = False
    sent_first = False
    pts0 = time.time()
    audio_cfg_sent = False
    video_origin_us = None
    video_frame_n = 0  # P5.2: rational PTS frame counter (native source only)
    audio_hold: list = []  # PCM queued until first video IDR shares the timeline

    def send(flags: int, payload: bytes, stream: int = STREAM_VIDEO, pts: int | None = None):
        nonlocal video_origin_us
        if pts is None:
            pts = int((time.time() - pts0) * 1_000_000)
        if stream == STREAM_VIDEO and video_origin_us is None and (flags & FLAG_IDR):
            video_origin_us = pts
        pkt = pack(flags, pts, payload, stream=stream)
        STATS["bytes"] += len(pkt)
        if stream == STREAM_AUDIO:
            STATS["audio_pkts"] += 1
            STATS["audio_bytes"] += len(payload)
        elif flags & FLAG_FRAME:
            # P1.5: frames actually pushed onto the wire (sentFps source).
            STATS["video_frames_sent"] = STATS.get("video_frames_sent", 0) + 1
        if flags & FLAG_CONFIG:
            STATS["config"] += 1
        if flags & FLAG_IDR:
            STATS["idr"] += 1
        dead = []
        with lock:
            for c in clients:
                try:
                    c.sendall(pkt)
                except Exception:
                    dead.append(c)
            for c in dead:
                try:
                    c.close()
                except Exception:
                    pass
                clients.remove(c)
                STATS["clients"] = len(clients)
                _note_disconnect("send failure")

    def flush_au():
        nonlocal au, au_has_vcl, au_irap, sent_first, video_frame_n
        if not au_has_vcl:
            au = bytearray()
            au_irap = False
            return
        if not sent_first:
            if not au_irap or not config:
                au = bytearray()
                au_has_vcl = False
                au_irap = False
                return
        if config and (not sent_first or au_irap):
            send(FLAG_CONFIG, bytes(config))
        # P5.2: for the native scene, the TZHL video PTS derives from the same
        # rational rate as the engine's clock — frame_n * den / num — so the
        # decoder's presentation timeline matches the encoder pacing exactly
        # (no pacing at 59.94 while stamping 60.0, and vice versa). Other
        # sources (golden/desktop) keep wall-clock PTS.
        if _STATE.get("source") == "native" and video_origin_us is not None:
            num, den = session_fraction()
            pts_us = video_origin_us + video_frame_n * den * 1_000_000 // num
        else:
            pts_us = None
        send(FLAG_FRAME | (FLAG_IDR if au_irap else 0), bytes(au), pts=pts_us)
        video_frame_n += 1
        sent_first = True
        au = bytearray()
        au_has_vcl = False
        au_irap = False

    def process_nal(nal: bytes):
        nonlocal config, au, au_has_vcl, au_irap, sent_first
        t, first = nal_info(nal)
        if t == NAL_VPS:
            config = bytearray()
            config.extend(nal)
            return
        if t in (NAL_SPS, NAL_PPS):
            config.extend(nal)
            return
        if t in VCL:
            if first and au_has_vcl:
                flush_au()
            au.extend(nal)
            au_has_vcl = True
            if t in IRAP:
                au_irap = True
        else:
            if t == NAL_AUD and au_has_vcl:
                flush_au()
            au.extend(nal)

    while not stop.is_set():
        try:
            item = _Q.get(timeout=1.0)
        except queue.Empty:
            continue
        kind = item[0]
        if kind == "reset":
            assembler.reset()
            config = bytearray()
            au = bytearray()
            au_has_vcl = False
            au_irap = False
            sent_first = False
            audio_cfg_sent = False
            video_origin_us = None
            video_frame_n = 0
            audio_hold = []
            pts0 = time.time()
            continue
        if kind == "eos":
            flush_au()
            continue
        if kind == "audio":
            # Gate the audio CONFIG until the first video IDR. If the AudioTrack
            # is created before video is ready it pre-rolls (playback head runs
            # ahead on silence), the clock races ahead of the video timeline and
            # every video frame is dropped as "too late" (deltaMs < -200).
            if video_origin_us is None:
                if len(audio_hold) < 2000:  # ~80 s of PCM at 40 ms/chunk
                    audio_hold.append((item[1], item[2]))
                continue
            if not audio_cfg_sent:
                cfg = AUDIO_CONFIG.pack(AUDIO_RATE, AUDIO_CH, 16, AUDIO_BYTES_PER_SEC)
                send(FLAG_CONFIG, cfg, stream=STREAM_AUDIO, pts=0)
                audio_cfg_sent = True
                # Flush the PCM that arrived before the first IDR on the shared
                # media timeline so no audio content is lost.
                for held_pts, held_chunk in audio_hold:
                    send(FLAG_FRAME, held_chunk, stream=STREAM_AUDIO, pts=held_pts)
                audio_hold = []
            send(FLAG_FRAME, item[2], stream=STREAM_AUDIO, pts=item[1])
            continue
        for nal in assembler.feed(item[1]):
            process_nal(nal)
    flush_au()


def _note_disconnect(reason: str) -> None:
    """Record a box disconnect + reconnect bookkeeping (P3.3)."""
    with _STATE_LOCK:
        now = time.time()
        if STATS.get("last_connected_at"):
            _STATE["lastDisconnectedAt"] = now
            _STATE["lastDisconnectReason"] = reason
            _STATE["reconnectAttempts"] = _STATE.get("reconnectAttempts", 0) + 1
        STATS["last_connected_at"] = None


def accept_loop(srv: socket.socket, clients: list, lock: threading.Lock, stop: threading.Event):
    srv.settimeout(0.5)
    while not stop.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)  # 1 MiB send buffer
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)  # 1 MiB recv buffer
        conn.settimeout(5.0)  # a stuck peer must not block the mux's sendall forever
        print("CLIENT", addr, flush=True)
        with lock:
            clients.append(conn)
            STATS["clients"] = len(clients)
        with _STATE_LOCK:
            now = time.time()
            if STATS.get("last_connected_at") is not None and len(clients) > 1:
                # a second concurrent client (not a reconnection)
                _STATE["lastDisconnectReason"] = "concurrent client"
            _STATE["lastConnectedAt"] = now
            STATS["last_connected_at"] = now


# ── HTTP control ────────────────────────────────────────────────────────
class _Ctrl(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        p = urlparse(self.path).path
        if p == "/cast/status":
            with _STATE_LOCK:
                st = _STATE
                prod = st["producer"]
                alive = prod is not None and prod.proc is not None and prod.proc.poll() is None
            self._json(200, {
                "activeAction": {
                    "scene": "cast.scene",
                    "short": "cast.youtube",
                    "interview": "cast.interviewCoach",
                    "desktop": "cast.desktop",
                }.get(st["source"], st["source"]),
                "activeSession": st["detail"],
                "source": st["source"],
                "detail": st["detail"],
                "activeScene": st["source"],
                "enhancementDecision": (st.get("enhancement") or {}).get("decision"),
                "enhancementStage": (st.get("enhancement") or {}).get("enhancementStatus"),
                "estimatedEnhancementSec": (st.get("enhancement") or {}).get("estimatedProcessingSec"),
                "playbackSource": (st.get("enhancement") or {}).get("playbackSource"),
                # The box enforces the single-decoder invariant (c2.amlogic.
                # hevc.decoder createCount=1); this host always emits one
                # stream into that decoder.
                "decoderCreateCount": 1,
                "decoderOwner": "DASHBOARD" if st.get("source") in ("scene", "native") else st.get("source"),
                "producer_alive": alive,
                "producer_pid": st["producer_pid"],
                "uptime": round(time.time() - st["started"], 1) if st["started"] else 0,
                "mediaStage": st.get("media_stage") or "idle",
                "mediaStageElapsed": round(time.time() - st["media_stage_started"], 1)
                if st.get("media_stage_started") else 0.0,
                "clients": STATS["clients"],
                # P3.3 reconnect/session state
                "sceneDesired": st["source"] == "native",
                "nativeProducerAlive": alive and st["source"] == "native",
                "tzhlListening": True,
                "boxConnected": STATS["clients"] >= 1,
                "clientCount": STATS["clients"],
                "lastConnectedAt": st.get("lastConnectedAt"),
                "lastDisconnectedAt": st.get("lastDisconnectedAt"),
                "lastDisconnectReason": st.get("lastDisconnectReason"),
                "reconnectAttempts": st.get("reconnectAttempts", 0),
                "boxStatsAgeSec": round(time.time() - st["box_stats_ts"], 1)
                if st.get("box_stats_ts") else None,
                "frames_sent": STATS["frames"],
                "idr_sent": STATS["idr"],
                "bytes_sent": STATS["bytes"],
                "audio_pkts": STATS["audio_pkts"],
                "audio_bytes": STATS["audio_bytes"],
                "audioState": "streaming" if STATS["audio_pkts"] else "idle",
                "desktop": dict(DESKTOP_STATS),
                "desktopCaptureState": "running" if (
                    st.get("source") == "desktop" and DESKTOP_STATS.get("frames_written", 0) > 0
                ) else "idle",
                "whisperState": st.get("whisper", "unknown"),
                "enhancement": st.get("enhancement") or {},
                "subtitleCueCount": st.get("subtitleCueCount") or 0,
                "subtitle": st.get("subtitle") or {},
                "translationProvider": (st.get("subtitle") or {}).get("translationProvider"),
                "translationStatus": (st.get("subtitle") or {}).get("translationStatus"),
                "videoResolution": "3840x2160",
                # P1.1: configured fps is a session parameter, never displayed
                # as a measured fps. Measured rates come from the engine metrics
                # (kind 2), the host send sampler, and the box stats reporter.
                "requestedFps": current_fps(),
                "videoFps": current_fps(),  # legacy alias
                # P5: the session rate is rational (num/den) end to end. Never
                # display 60000/1001 as 60.0 in telemetry; fpsNumerator/
                # fpsDenominator are the canonical representation.
                "fpsNumerator": session_fraction()[0],
                "fpsDenominator": session_fraction()[1],
                "rateMode": "DISPLAY_NATIVE",
                "producedFps": (st.get("native_metrics") or {}).get("produced_fps"),
                "encodedFps": (st.get("native_metrics") or {}).get("encoded_fps"),
                "engineFrames": (st.get("native_metrics") or {}).get("frames"),
                "engineAUs": (st.get("native_metrics") or {}).get("aus"),
                "engineDeadlineMisses": (st.get("native_metrics") or {}).get("deadline_misses"),
                "engineQueueDepth": (st.get("native_metrics") or {}).get("queue_depth"),
                "engineRenderMs": (st.get("native_metrics") or {}).get("render_ms"),
                "engineConvertMs": (st.get("native_metrics") or {}).get("convert_ms"),
                "engineEncodeMs": (st.get("native_metrics") or {}).get("encode_ms"),
                "engineFrameMs": (st.get("native_metrics") or {}).get("frame_ms"),
                "readbackBytesPerFrame": (st.get("native_metrics") or {}).get("readback_bytes_per_frame", 0),
                "sentFps": STATS.get("sent_fps", 0.0),
                "sentFrames": STATS.get("video_frames_sent", 0),
                "boxOutputFps": (st.get("box_stats") or {}).get("outputFps"),
                "box": st.get("box_stats") or {},
                "bsf": {
                    "enabled": BSF_STATS["enabled"],
                    "inputAUs": BSF_STATS["input_aus"],
                    "outputChunks": BSF_STATS["output_chunks"],
                    "latencyP50Ms": _bsf_p50_p95(BSF_STATS["latency_ms"])[0],
                    "latencyP95Ms": _bsf_p50_p95(BSF_STATS["latency_ms"])[1],
                    "queueDepth": BSF_STATS["queue_depth"],
                    "backlog": BSF_STATS["backlog"],
                },
                "orientation": st.get("orientation") or {},
                "priorityGuard": _priority_status(),
                "lastError": st["last_error"],
                "last_error": st["last_error"],
            })
        else:
            self._json(404, {"error": "unknown"})

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            body = {}
        p = urlparse(self.path).path
        if p == "/cast/boxstats":
            # Box app -> host telemetry (P1.5/P3.3): decoder createCount,
            # frames in/out/dropped, computed outputFps. Stored for status.
            if isinstance(body, dict):
                with _STATE_LOCK:
                    _STATE["box_stats"] = body
                    _STATE["box_stats_ts"] = time.time()
                self._json(200, {"ok": True})
                return
        action = str(body.get("action") or "")
        result = handle_cast_action(action, body)
        code = 200 if result.get("ok") else 400
        self._json(code, result)

    def log_message(self, *args):  # noqa: A003
        pass


def handle_cast_action(action: str, body: dict) -> dict:
    """Route a cast action. Heavy scenarios run in a background thread."""
    if action in ("cast.scene", "cast.stop"):
        # P1.1/P5: fps is an explicit per-session parameter. Accept either a
        # float (fps) or an exact rational (fpsNumerator + fpsDenominator) so
        # 60000/1001 and 60/1 pass through unapproximated. The engine, the
        # state-feed pacing, and status all derive from the stored rational.
        num = body.get("fpsNumerator")
        den = body.get("fpsDenominator")
        if num is not None and den is not None:
            set_session_fps(num, den)
        else:
            set_session_fps(body.get("fps") if body.get("fps") is not None
                            else os.environ.get("TOASTOVAC_FPS", "30"))
        if action == "cast.scene":
            # P3.1: starting the native scene also places the box into the
            # live-HDR receiver/activity via the safe control path (adb am
            # start of the HOME activity) — no manual navigation. The
            # receiver auto-reconnects if the host restarts (P3.2).
            threading.Thread(target=_activate_box_live, daemon=True).start()
        ok = switch_source("native", "live")
        n, d = session_fraction()
        return {"ok": ok, "source": "native", "fps": current_fps(),
                "fpsNumerator": n, "fpsDenominator": d}
    if action == "cast.youtube":
        url = str(body.get("url") or body.get("video_id") or "")
        import re as _re
        m = _re.search(r"[A-Za-z0-9_-]{6,}", url)
        video_id = m.group(0) if m else ""
        if not video_id:
            return {"ok": False, "error": "missing video_id/url"}
        # P5 playback intent: explicit quality requests (cinematic/enhance/
        # best) win over latency; everything else defaults to PLAY_NOW.
        intent = str(body.get("intent") or "PLAY_NOW").upper()
        quality = str(body.get("quality") or "").lower()
        if quality in ("cinematic", "enhance", "enhance this", "upscale", "best", "best quality"):
            intent = "PREPARE_CINEMATIC"
        threading.Thread(target=_run_korean_short, args=(video_id, intent), daemon=True).start()
        return {"ok": True, "source": "youtube", "video_id": video_id,
                "status": "preparing", "intent": intent}
    if action == "cast.ab":
        url = str(body.get("url") or body.get("video_id") or "")
        import re as _re
        m = _re.search(r"[A-Za-z0-9_-]{6,}", url)
        video_id = m.group(0) if m else ""
        preset = str(body.get("preset") or "auto").lower()
        if not video_id:
            return {"ok": False, "error": "missing video_id/url"}
        if preset not in ("auto", "balanced", "cinematic"):
            preset = "auto"
        warm = bool(body.get("prepareAll"))
        threading.Thread(target=_run_ab, args=(video_id, preset, warm),
                         daemon=True).start()
        return {"ok": True, "source": "ab", "video_id": video_id,
                "preset": preset, "prepareAll": warm, "status": "preparing"}
    if action == "cast.cin":
        url = str(body.get("url") or body.get("video_id") or "")
        import re as _re
        m = _re.search(r"[A-Za-z0-9_-]{6,}", url)
        video_id = m.group(0) if m else ""
        step = str(body.get("step") or "hdr").lower()
        if not video_id:
            return {"ok": False, "error": "missing video_id/url"}
        if step not in ("hdr", "cleanup", "detail", "color", "interp"):
            step = "hdr"
        threading.Thread(target=_run_cin, args=(video_id, step), daemon=True).start()
        return {"ok": True, "source": "cin", "video_id": video_id,
                "step": step, "status": "preparing"}

    if action == "cast.showroom":
        url = str(body.get("url") or body.get("video_id") or "")
        import re as _re
        m = _re.search(r"[A-Za-z0-9_-]{6,}", url)
        video_id = m.group(0) if m else ""
        name = str(body.get("name") or "compare").lower()
        if not video_id:
            return {"ok": False, "error": "missing video_id/url"}
        if name not in ("compare", "a", "b", "c"):
            return {"ok": False, "error": f"unknown showroom asset {name}"}
        ok = switch_source("showroom", f"{video_id}:{name}")
        return {"ok": ok, "source": "showroom", "video_id": video_id,
                "name": name, "status": "streaming" if ok else "failed"}
    if action == "cast.interviewCoach":
        topic = str(body.get("topic") or "Python")
        duration = float(body.get("durationSec") or 75)
        threading.Thread(target=_run_interview, args=(topic, duration), daemon=True).start()
        return {"ok": True, "source": "interview", "status": "starting"}
    if action == "cast.desktop":
        ok = switch_source("desktop", "desktop")
        return {"ok": ok, "source": "desktop"}
    return {"ok": False, "error": f"unknown action {action}"}


def rate_sampler(stop: threading.Event):
    """Compute measured sentFps over a rolling window (P1.5).

    Never reads the configured FPS — it counts TZHL video frames actually
    pushed onto the wire and divides by wall time. The window is 5 s (not 2 s)
    so a single TCP-buffered frame crossing a window edge cannot read as a
    frame-rate drop (verified: a 2 s window showed 58.47 while the box
    received 60.00/s exactly — boundary artifact, zero wire loss).
    """
    last_t = time.perf_counter()
    last_n = STATS.get("video_frames_sent", 0)
    while not stop.is_set():
        time.sleep(5)
        now = time.perf_counter()
        n = STATS.get("video_frames_sent", 0)
        dt = max(0.001, now - last_t)
        STATS["sent_fps"] = round((n - last_n) / dt, 2)
        last_t, last_n = now, n


def _activate_box_live() -> None:
    """Best-effort box activation (P3.1): place the box into the HOME
    LiveHdrActivity through the safe control path (adb am start). Never
    fatal — the receiver reconnects on its own; this only removes manual
    navigation. TOASTOVAC_BOX overrides the box adb endpoint."""
    box = os.environ.get("TOASTOVAC_BOX", "192.168.1.122:5555")
    try:
        import subprocess as _sp
        r = _sp.run(
            ["adb", "-s", box, "shell", "am", "start",
             "-n", "ai.toastovac.tv/.LiveHdrActivity"],
            capture_output=True, text=True, timeout=15)
        print(f"BOX_ACTIVATE rc={r.returncode} {r.stdout.strip()[:200]}", flush=True)
    except Exception as e:
        print(f"BOX_ACTIVATE_SKIP {e}", flush=True)


def _set_stage(stage: str) -> None:
    """Expose a coarse media-prep stage so the UI never looks frozen (P7.1)."""
    with _STATE_LOCK:
        _STATE["media_stage"] = stage
        _STATE["media_stage_started"] = time.time()
    print(f"MEDIA_STAGE {stage}", flush=True)


def _effective_playback_path(video_id: str) -> Path:
    """The media that will actually play: the enhanced derivative when active."""
    vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
    with _STATE_LOCK:
        enh = (_STATE.get("enhancement") or {}).get("playbackPath")
    if enh and Path(enh).is_file():
        return Path(enh)
    return vd / "source.mp4"


def _subtitle_cue_count(vd: Path) -> int:
    try:
        srt = vd / "cs_subtitles.srt"
        if not srt.is_file():
            return 0
        text = srt.read_text(encoding="utf-8", errors="replace")
        return sum(1 for ln in text.splitlines() if ln.strip().isdigit())
    except Exception:
        return 0


def _run_korean_short(video_id: str, intent: str = "PLAY_NOW") -> None:
    """Ensure transcript+subtitles exist, then switch to the short source."""
    try:
        _set_stage("PREPARING")
        from jarvis.everywhere import yt_download
        dl = yt_download.get_downloader()
        job = dl.start(video_id)
        deadline = time.time() + 1200
        while job.state not in ("ready", "error") and time.time() < deadline:
            time.sleep(3)
        if job.state != "ready":
            with _STATE_LOCK:
                _STATE["last_error"] = f"download failed: {job.message}"
            _set_stage("FAILED")
            return
        # transcript + czech subtitles if missing
        vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
        _set_stage("ENHANCING")
        try:
            from jarvis.everywhere.intel_enhance import decide_and_maybe_enhance
            enh = decide_and_maybe_enhance(
                vd / "source.mp4", preset="auto", intent=intent,
                schedule_background=(intent == "PLAY_NOW"))
            src_meta = enh.get("source") or {}
            with _STATE_LOCK:
                _STATE["enhancement"] = {
                    "decision": enh.get("decision"),
                    "reason": enh.get("reason"),
                    "playbackSource": enh.get("playbackSource"),
                    "enhancementStatus": enh.get("enhancementStatus"),
                    "tool": enh.get("tool"),
                    "device": enh.get("device"),
                    "cacheHit": enh.get("cacheHit"),
                    "processingSec": enh.get("processingSec"),
                    "fallbackReason": enh.get("fallbackReason"),
                    "playbackPath": enh.get("playbackPath"),
                    "intent": enh.get("intent") or intent,
                    "estimatedProcessingSec": enh.get("estimatedProcessingSec"),
                    "actualProcessingSec": enh.get("actualProcessingSec"),
                    "backgroundCacheScheduled": enh.get("backgroundCacheScheduled"),
                    "sourceWidth": src_meta.get("width"),
                    "sourceHeight": src_meta.get("height"),
                    "outputWidth": (enh.get("outputMeta") or {}).get("raw", {}).get("streams", [{}])[0].get("width")
                    if enh.get("outputMeta") and enh.get("outputMeta").get("ok") else None,
                    "outputHeight": (enh.get("outputMeta") or {}).get("raw", {}).get("streams", [{}])[0].get("height")
                    if enh.get("outputMeta") and enh.get("outputMeta").get("ok") else None,
                    "audioMode": enh.get("audioMode"),
                    "audioPresent": enh.get("audioPresent"),
                    "nvidiaUsed": enh.get("nvidiaUsed"),
                }
            print("ENHANCE", enh.get("decision"), enh.get("reason"),
                  enh.get("playbackSource"), "intent=", intent, flush=True)
        except Exception as e:
            with _STATE_LOCK:
                _STATE["enhancement"] = {
                    "enhancementStatus": "FAILED",
                    "playbackSource": "ORIGINAL",
                    "fallbackReason": str(e),
                }
            print("ENHANCE_FAIL", e, flush=True)
        if not (vd / "cs_subtitles.srt").is_file():
            _set_stage("SUBTITLES")
            from jarvis.everywhere import korean_translate
            from jarvis.everywhere.whisper_worker import get_worker
            w = get_worker()
            with _STATE_LOCK:
                _STATE["whisper"] = w.status()
            tr = w.transcribe_file(str(vd / "audio16k.wav") if (vd / "audio16k.wav").is_file()
                                   else str(vd / "source.mp4"), language="ko")
            from jarvis.everywhere import korean_short
            korean_short.segments_to_srt(tr["segments"], vd / "ko_original.srt")
            (vd / "ko_transcript.json").write_text(
                json.dumps(tr, ensure_ascii=False, indent=2), encoding="utf-8")
            cfg = korean_translate.load_cfg()
            ident = korean_translate.provider_identity(cfg)
            media_dur = korean_translate.media_duration_of(_effective_playback_path(video_id))
            try:
                cz, report = korean_translate.translate_pipeline(
                    cfg, tr["segments"], "Czech", media_duration_sec=media_dur)
                korean_translate.write_srt(cz, vd / "cs_subtitles.srt")
                korean_translate.write_quality_report(report, vd / "subtitle_quality.json")
                with _STATE_LOCK:
                    _STATE["subtitle"] = {
                        "state": "READY",
                        "translationProvider": report.get("configuredProvider"),
                        "actualProvider": report.get("actualProvider"),
                        "translationStatus": report.get("translationStatus"),
                        "model": report.get("model"),
                        "cueCount": report.get("cueCount"),
                        "repairCount": report.get("repairCount"),
                        "droppedCueCount": report.get("droppedCueCount"),
                        "clampedCueCount": report.get("clampedCueCount"),
                    }
            except korean_translate.TranslationProviderError as e:
                # P0.1 no silent fallback: the configured provider is down.
                with _STATE_LOCK:
                    _STATE["subtitle"] = {
                        "state": "PROVIDER_UNAVAILABLE",
                        "translationStatus": "PROVIDER_UNAVAILABLE",
                        "configuredProvider": ident.get("label"),
                        "actualProvider": "NONE",
                        "model": ident.get("model"),
                        "error": str(e),
                    }
                print("SUBTITLE_PROVIDER_UNAVAILABLE", ident.get("label"), e, flush=True)
        else:
            # Cached SRT present: deterministic media-end hygiene only (no LLM),
            # so a stale cache can never show cues past the media end on TV.
            from jarvis.everywhere import korean_translate as _kt
            try:
                media_dur = _kt.media_duration_of(_effective_playback_path(video_id))
                if media_dur > 0:
                    stats = _kt.apply_media_end_to_srt(vd / "cs_subtitles.srt", media_dur)
                    print("SUBTITLE_HYGIENE", stats, flush=True)
            except Exception as e:
                print("SUBTITLE_HYGIENE_FAIL", e, flush=True)
            sub_state = {"state": "READY", "hygiene": True}
            qf = vd / "subtitle_quality.json"
            if qf.is_file():
                try:
                    q = json.loads(qf.read_text(encoding="utf-8"))
                    sub_state.update({
                        "translationProvider": q.get("configuredProvider"),
                        "actualProvider": q.get("actualProvider"),
                        "translationStatus": q.get("translationStatus"),
                        "model": q.get("model"),
                        "cueCount": q.get("cueCount"),
                        "repairCount": q.get("repairCount"),
                        "droppedCueCount": q.get("droppedCueCount"),
                        "clampedCueCount": q.get("clampedCueCount"),
                    })
                except Exception as e:
                    print("SUBTITLE_QUALITY_READ_FAIL", e, flush=True)
            with _STATE_LOCK:
                _STATE["subtitle"] = sub_state
        with _STATE_LOCK:
            _STATE["subtitleCueCount"] = _subtitle_cue_count(vd)
        _set_stage("READY")
        ok = switch_source("short", video_id)
        if ok:
            _set_stage("PLAYING")
        print("KOREAN_SHORT_READY", video_id, ok, flush=True)
    except Exception as e:
        with _STATE_LOCK:
            _STATE["last_error"] = f"korean short failed: {e}"
        _set_stage("FAILED")
        print("KOREAN_SHORT_FAIL", repr(e), flush=True)


_CINEMATIC_LADDER = ("auto", "balanced", "cinematic")


_CINEMATIC_STEPS = ("hdr", "cleanup", "detail", "color", "interp")


def _run_cin(video_id: str, step: str) -> None:
    """Cinematic ladder: ensure the processed derivative, render the split
    comparison (HDR signaling for HDR steps), then stream it."""
    try:
        _set_stage("PREPARING")
        from cinematic import process_step
        _set_stage("PROCESSING")
        res = process_step(video_id, step)
        if not res.get("ok"):
            with _STATE_LOCK:
                _STATE["last_error"] = (
                    f"cast.cin: {res.get('error') or 'processing failed'}")
            _set_stage("FAILED")
            return
        _set_stage("RENDERING")
        right = Path(str(res["path"]))
        hdr = bool(res.get("hdr"))
        # A/B isolates THIS step: left = previous ladder step's output (or
        # source.mp4 for the first step), so each split compares step input
        # vs step output — never ORIGINAL vs a later step.
        ed = right.parent
        left_path = None
        left_label = "ORIGINAL"
        if step in _CINEMATIC_STEPS:
            idx = _CINEMATIC_STEPS.index(step)
            if idx > 0:
                prev = ed / f"cin_{_CINEMATIC_STEPS[idx - 1]}.mp4"
                if prev.is_file():
                    left_path = prev
                    left_label = _CINEMATIC_STEPS[idx - 1].upper()
        if step == "detail":
            right_label = "DETAIL + CAS 0.25"
        elif step == "color":
            right_label = "COLOR FINISH"
        else:
            right_label = step.upper()
        out = _render_compare(video_id, right, f"ab_{step}", right_label,
                              hdr=hdr, left_path=left_path,
                              left_label=left_label)
        if out is None:
            with _STATE_LOCK:
                _STATE["last_error"] = f"cast.cin: render failed ({step})"
            _set_stage("FAILED")
            return
        ok = switch_source("cin", f"{video_id}:{step}")
        _set_stage("PLAYING" if ok else "FAILED")
        print(f"CIN_READY {video_id} {step} hdr={hdr} ok={ok} "
              f"ladder={res.get('ladder')}", flush=True)
    except Exception as e:
        with _STATE_LOCK:
            _STATE["last_error"] = f"cast.cin failed: {e}"
        _set_stage("FAILED")
        print("CIN_FAIL", repr(e), flush=True)


def _run_ab(video_id: str, preset: str, warm_others: bool = False) -> None:
    """A/B compare: ensure the derivative for `preset`, switch to the split
    source. With warm_others, kick off background prep of the remaining
    ladder presets so 'next' is instant."""
    try:
        _set_stage("PREPARING")
        vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
        from jarvis.everywhere.intel_enhance import decide_and_maybe_enhance
        _set_stage("ENHANCING")
        rec = decide_and_maybe_enhance(vd / "source.mp4", preset=preset,
                                       intent="PREPARE_CINEMATIC")
        if rec.get("playbackSource") != "ENHANCED" or not rec.get("playbackPath"):
            with _STATE_LOCK:
                _STATE["last_error"] = (
                    f"cast.ab: enhancement unavailable ({preset}): "
                    f"{rec.get('reason') or rec.get('error')}")
            _set_stage("FAILED")
            return
        _set_stage("RENDERING")
        out = _render_ab_file(video_id, preset)
        if out is None:
            with _STATE_LOCK:
                _STATE["last_error"] = f"cast.ab: render failed ({preset})"
            _set_stage("FAILED")
            return
        ok = switch_source("ab", f"{video_id}:{preset}")
        _set_stage("PLAYING" if ok else "FAILED")
        print(f"AB_READY {video_id} {preset} ok={ok}", flush=True)
        if warm_others:
            for other in _CINEMATIC_LADDER:
                if other == preset:
                    continue
                threading.Thread(target=_prep_one, args=(video_id, other),
                                 daemon=True).start()
    except Exception as e:
        with _STATE_LOCK:
            _STATE["last_error"] = f"cast.ab failed: {e}"
        _set_stage("FAILED")
        print("AB_FAIL", repr(e), flush=True)


def _prep_one(video_id: str, preset: str) -> None:
    try:
        from jarvis.everywhere.intel_enhance import decide_and_maybe_enhance
        vd = Path(os.environ.get("LOCALAPPDATA", "")) / "VIVERRA" / "Toastovac" / "videos" / video_id
        rec = decide_and_maybe_enhance(vd / "source.mp4", preset=preset,
                                       intent="PREPARE_CINEMATIC")
        print(f"PREP_ONE {preset} {rec.get('playbackSource')} "
              f"{rec.get('enhancementStatus')}", flush=True)
        if rec.get("playbackSource") == "ENHANCED":
            _render_ab_file(video_id, preset)
    except Exception as e:
        print(f"PREP_ONE_FAIL {preset} {e}", flush=True)


def _run_interview(topic: str, duration: float) -> None:
    try:
        from interview_scene import InterviewTvSession
        session = InterviewTvSession(topic=topic, duration_sec=duration)
        ok = switch_source("interview", topic)
        if ok:

            def _watch_render():
                base_idr = STATS["idr"]
                t_end = time.time() + duration + 30
                while time.time() < t_end:
                    time.sleep(2)
                    with _STATE_LOCK:
                        src = _STATE["source"]
                    if src == "interview" and STATS["clients"] >= 1 \
                            and STATS["idr"] > base_idr:
                        session.note_rendered_on_box()
                        print("INTERVIEW_RENDERED_ON_BOX", flush=True)
                        return

            threading.Thread(target=_watch_render, daemon=True).start()
        ok2 = session.start_on_host()
        print("INTERVIEW_DONE ok=", ok2, flush=True)
    except Exception as e:
        with _STATE_LOCK:
            _STATE["last_error"] = f"interview failed: {e}"
        print("INTERVIEW_FAIL", repr(e), flush=True)


def control_server(stop: threading.Event):
    httpd = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), _Ctrl)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    print(f"CTRL http://0.0.0.0:{CONTROL_PORT} /cast", flush=True)
    while not stop.is_set():
        time.sleep(1)
    httpd.shutdown()


# ── priority guard: green light for the stream, yellow for competitors ──
# While a cast is actively streaming, watch the measured wire rate
# (STATS['sent_fps']). If it falls below ~85% of the expected rate for a
# couple of consecutive samples, the encoder is being CPU-starved (e.g. a
# chromium build pegging the machine) — demote the biggest non-pipeline CPU
# consumers to BelowNormal ("yellow light"), sticky for their lifetime.
# Everything is restored once streaming stops. Never touches our own
# pipeline (cast_host, ffmpeg, native engine) or Windows core processes.
_PRIORITY_LOCK = threading.Lock()
_PRIORITY_STATE = {"state": "idle", "starveSamples": 0, "expectedFps": 0.0,
                   "sentFps": 0.0, "throttled": [], "lastAction": ""}
_THROTTLED: dict[int, tuple] = {}  # pid -> (name, original priority class)
_PRIORITY_STARVE_MIN = 0.85
_PRIORITY_STARVE_SAMPLES = 2
_PRIORITY_CPU_MIN_PCT = 18.0
_PRIORITY_MAX_DEMOTIONS = 8
_PRIORITY_CORE = {"System", "Idle", "Registry", "Memory Compression",
                  "Secure System", "dwm", "csrss", "winlogon", "services",
                  "lsass", "fontdrvhost"}
_PRIORITY_MINE = {"ffmpeg", "ffprobe", "native_engine", "toastovac_gpu"}
_NORMAL_PRIO = 0x20
_BELOW_NORMAL_PRIO = 0x4000
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_SET_INFORMATION = 0x0200
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _prio_set(pid: int, cls: int) -> bool:
    import ctypes
    try:
        h = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_INFORMATION | _PROCESS_SET_INFORMATION, False, pid)
        if not h:
            h = ctypes.windll.kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_SET_INFORMATION,
                False, pid)
        if not h:
            return False
        try:
            return bool(ctypes.windll.kernel32.SetPriorityClass(h, cls))
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return False


def _prio_get(pid: int) -> int | None:
    import ctypes
    try:
        h = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_INFORMATION, False, pid)
        if not h:
            h = ctypes.windll.kernel32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            return int(ctypes.windll.kernel32.GetPriorityClass(h))
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None


def _our_pids() -> set:
    """cast_host + its producer children (ffmpeg / native engine)."""
    pids = {os.getpid()}
    try:
        import subprocess as _sp
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             f"Where-Object {{ $_.ParentProcessId -eq {os.getpid()} }} | "
             "Select-Object -ExpandProperty ProcessId"],
            capture_output=True, text=True, timeout=10)
        for ln in out.stdout.splitlines():
            ln = ln.strip()
            if ln.isdigit():
                pids.add(int(ln))
    except Exception:
        pass
    return pids


def _top_cpu_consumers(min_pct: float, limit: int) -> list:
    """[(pid, name, pct)] of the biggest instantaneous CPU users."""
    try:
        import subprocess as _sp
        cmd = ("Get-CimInstance Win32_PerfFormattedData_PerfProc_Process | "
               f"Where-Object {{ $_.PercentProcessorTime -gt {min_pct} -and "
               "$_.IDProcess -ne 0 }} | Sort-Object PercentProcessorTime -Descending | "
               f"Select-Object -First {limit} IDProcess, Name, PercentProcessorTime | "
               "ConvertTo-Json -Compress")
        out = _sp.run(["powershell", "-NoProfile", "-Command", cmd],
                      capture_output=True, text=True, timeout=20)
        raw = json.loads(out.stdout or "[]")
        items = raw if isinstance(raw, list) else [raw]
        return [(int(i["IDProcess"]), str(i["Name"]), float(i["PercentProcessorTime"]))
                for i in items if i.get("IDProcess")]
    except Exception:
        return []


def _priority_demote_competitors():
    ours = _our_pids()
    with _PRIORITY_LOCK:
        if _PRIORITY_STATE["state"] == "idle":
            _PRIORITY_STATE["state"] = "armed"
        demoted = []
        for pid, name, pct in _top_cpu_consumers(
                _PRIORITY_CPU_MIN_PCT, _PRIORITY_MAX_DEMOTIONS + 4):
            if pid in ours or pid in _THROTTLED:
                continue
            if name in _PRIORITY_CORE or name in _PRIORITY_MINE:
                continue
            cur = _prio_get(pid)
            if cur is None or cur == _BELOW_NORMAL_PRIO:
                continue
            if _prio_set(pid, _BELOW_NORMAL_PRIO):
                _THROTTLED[pid] = (name, cur)
                demoted.append(f"{name}({pct:.0f}%)")
                if len(demoted) >= _PRIORITY_MAX_DEMOTIONS:
                    break
        if demoted:
            _PRIORITY_STATE["throttled"] = sorted(
                {n for n, _ in _THROTTLED.values()})
            _PRIORITY_STATE["lastAction"] = (
                f"demoted {len(demoted)}: {', '.join(demoted)}")
            print(f"PRIORITY_DEMOTE {', '.join(demoted)}", flush=True)


def _priority_restore_all():
    with _PRIORITY_LOCK:
        if not _THROTTLED:
            return
        restored = 0
        for pid, (name, orig) in list(_THROTTLED.items()):
            cur = _prio_get(pid)
            if cur == _BELOW_NORMAL_PRIO and _prio_set(pid, orig):
                restored += 1
            _THROTTLED.pop(pid, None)  # dead or restored: forget it
        _PRIORITY_STATE["throttled"] = []
        _PRIORITY_STATE["state"] = "idle"
        if restored:
            _PRIORITY_STATE["lastAction"] = f"restored {restored} to normal"
            print(f"PRIORITY_RESTORE {restored}", flush=True)


def priority_guard(stop: threading.Event):
    """Semaphore: green light for the stream, yellow for competitors."""
    starve = 0
    idle = 0
    while not stop.is_set():
        time.sleep(5)
        try:
            with _STATE_LOCK:
                source = _STATE.get("source")
                producer = _STATE.get("producer")
            alive = (producer is not None and producer.proc is not None
                     and producer.proc.poll() is None)
            active = alive and STATS.get("clients", 0) >= 1
            if source in ("short", "ab", "cin"):
                expected = 60.0  # short/ab/cin normalize to CFR 60
            elif source in ("native", "scene"):
                expected = current_fps()
            else:
                expected = None
            if not active or expected is None or expected <= 0:
                idle += 1
                starve = 0
                if idle >= 2:
                    _priority_restore_all()
                continue
            idle = 0
            sent = STATS.get("sent_fps", 0.0)
            with _PRIORITY_LOCK:
                _PRIORITY_STATE["sentFps"] = round(sent, 2)
                _PRIORITY_STATE["expectedFps"] = round(expected, 3)
                if _PRIORITY_STATE["state"] == "idle":
                    _PRIORITY_STATE["state"] = "armed"
            if sent < _PRIORITY_STARVE_MIN * expected:
                starve += 1
            else:
                starve = 0
            with _PRIORITY_LOCK:
                _PRIORITY_STATE["starveSamples"] = starve
            if starve >= _PRIORITY_STARVE_SAMPLES:
                _priority_demote_competitors()
                starve = 0
        except Exception as e:
            print(f"PRIORITY_GUARD_ERR {e}", flush=True)


def _priority_status() -> dict:
    with _PRIORITY_LOCK:
        return dict(_PRIORITY_STATE)


def _thread_excepthook(args):
    """Log any unhandled thread exception fully, then fail fast.

    A dead mux/pump thread with a live main thread leaves the TV frozen
    while the process looks healthy — worse than a clean exit. Log the
    traceback (persistent log) and terminate so supervision can restart.
    """
    print(f"THREAD_CRASH {getattr(args.thread, 'name', '?')}: "
          f"{args.exc_type.__name__}: {args.exc_value}", flush=True)
    if args.exc_traceback:
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)
    sys.exit(3)


def main():
    threading.excepthook = _thread_excepthook
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=LISTEN_PORT)
    ap.add_argument("--source", default="scene")
    ap.add_argument("--detail", default="live")
    args = ap.parse_args()

    stop = threading.Event()
    clients: list = []
    lock = threading.Lock()

    threading.Thread(target=mux_loop, args=(clients, lock, stop), daemon=True).start()
    threading.Thread(target=control_server, args=(stop,), daemon=True).start()
    threading.Thread(target=rate_sampler, args=(stop,), daemon=True).start()
    threading.Thread(target=priority_guard, args=(stop,), daemon=True,
                     name="priority-guard").start()

    ok = switch_source(args.source, args.detail)
    if not ok:
        print("initial source failed", flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, args.port))
    srv.listen(4)
    print(f"LISTEN {LISTEN_HOST}:{args.port} cast-host (sources: scene/short/interview/desktop)", flush=True)
    threading.Thread(target=accept_loop, args=(srv, clients, lock, stop), daemon=True).start()

    try:
        while True:
            time.sleep(2)
            dt = max(0.001, time.time() - STATS["start"])
            mbps = STATS["bytes"] * 8 / dt / 1e6
            with _STATE_LOCK:
                src = _STATE["source"]
                detail = _STATE["detail"]
            print(f"stat src={src}:{detail} idr={STATS['idr']} cfg={STATS['config']} "
                  f"clients={STATS['clients']} avg={mbps:.2f}Mbps "
                  f"t={time.time()-STATS['start']:.0f}s", flush=True)
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        sys.exit(2)
    stop.set()
    try:
        srv.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()