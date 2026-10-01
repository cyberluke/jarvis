#!/usr/bin/env python3
"""Live 4K60 HDR10 sender: Scene → RGB48 → hevc_qsv Main10 → TCP :8768.

TEMP path: CPU raster + ffmpeg stdin (copy-heavy). Intel Xe is the encoder.
No NVENC. Persistent TCP. Proper Annex-B AU assembly.

Wire sequence (deterministic):
  1. CONFIG packet  : VPS+SPS+PPS            (FLAG_CONFIG)
  2. first IRAP AU  : whole first IDR/CRA/BLA picture (FLAG_IDR|FLAG_FRAME)
  3. every picture  : one whole access unit per packet (FLAG_FRAME, +FLAG_IDR on IRAP)

Rules enforced:
  - AUs whose first picture is not IRAP are dropped (undecodable from cold).
  - One picture is never split across packets.
  - Two pictures are never merged into one packet.
  - Parameter sets are re-sent before every IRAP picture.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from hdr_scene import W, H, render_frame  # noqa: E402
from protocol import FLAG_CONFIG, FLAG_FRAME, FLAG_IDR, pack  # noqa: E402

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8768
FPS = 15
IDR_EVERY = 15
BITRATE = "18M"
MAXRATE = "28M"
BUFSIZE = "40M"

STATS = {
    "frames": 0,
    "bytes": 0,
    "render_ms": 0.0,
    "clients": 0,
    "idr": 0,
    "config": 0,
    "start": time.time(),
}

NAL_VPS, NAL_SPS, NAL_PPS = 32, 33, 34
NAL_AUD = 35
IRAP = {16, 17, 18, 19, 20, 21, 22, 23}
VCL = set(range(0, 32))


def preroll_dir() -> Path:
    d = Path(os.environ.get("TEMP", "/tmp")) / "kilo" / "live_hdr_preroll"
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_preroll(n: int = 30) -> list[Path]:
    """Rasterize N unique 4K PQ frames once. Encoder then loops them at FPS."""
    d = preroll_dir()
    paths = []
    for i in range(n):
        p = d / f"f{i:03d}.rgb48"
        if not p.is_file() or p.stat().st_size < W * H * 6:
            t = i / max(1, FPS)
            frame = render_frame(t, {"pre": i, "pq": "1"})
            p.write_bytes(frame.tobytes())
            print("PREROLL", p.name, flush=True)
        paths.append(p)
    return paths


def start_encoder() -> subprocess.Popen:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb48le",
        "-s",
        f"{W}x{H}",
        "-r",
        str(FPS),
        "-i",
        "pipe:0",
        "-vf",
        "format=p010le",
        "-c:v",
        "hevc_qsv",
        "-profile:v",
        "main10",
        "-preset",
        "veryfast",
        "-bf",
        "0",
        "-g",
        str(IDR_EVERY),
        "-forced_idr",
        "1",
        "-async_depth",
        "2",
        "-aud",
        "1",
        "-adaptive_i",
        "0",
        "-scenario",
        "livestreaming",
        "-b:v",
        BITRATE,
        "-maxrate",
        MAXRATE,
        "-bufsize",
        BUFSIZE,
        "-color_primaries",
        "bt2020",
        "-color_trc",
        "smpte2084",
        "-colorspace",
        "bt2020nc",
        "-color_range",
        "tv",
        "-f",
        "hevc",
        "-bsf:v",
        "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
        "pipe:1",
    ]
    print("ENC", " ".join(cmd), flush=True)
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def render_loop(enc: subprocess.Popen, stop: threading.Event):
    from hdr_scene import LiveScene
    scene = LiveScene()
    print("LIVE scene initialized", flush=True)
    t0 = time.perf_counter()
    n = 0
    while not stop.is_set():
        target = t0 + n / FPS
        now = time.perf_counter()
        if now < target:
            time.sleep(target - now)
        t = n / FPS
        t_r = time.perf_counter()
        blob = scene.render(t, {"live": n, "fps": FPS, "pq": "1"}).tobytes()
        dt = time.perf_counter() - t_r
        STATS["render_ms"] = dt * 1000.0
        try:
            enc.stdin.write(blob)
        except BrokenPipeError:
            break
        n += 1
        STATS["frames"] = n
    try:
        enc.stdin.close()
    except Exception:
        pass


def annexb_nals(buf: bytes):
    """Yield each Annex-B NAL (start code included) from a byte buffer."""
    n = len(buf)
    i = 0
    starts = []
    while i < n:
        if buf[i] != 0:
            i += 1
            continue
        j = i
        while j < n and buf[j] == 0:
            j += 1
        if j < n and buf[j] == 1:
            if j - i >= 2:  # valid start code needs at least 00 00 01
                starts.append(i)
            i = j + 1
        else:
            i = max(i + 1, j)
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else n
        yield bytes(buf[s:e])


def nal_info(nal: bytes):
    """Return (nal_type, first_slice_flag) for an Annex-B NAL."""
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


def iter_nals(pipe):
    """Scan the encoder pipe for NALs, safe across read boundaries."""
    carry = bytearray()
    while True:
        chunk = pipe.read(64 * 1024)
        if not chunk:
            if carry:
                yield from annexb_nals(bytes(carry))
            return
        carry.extend(chunk)
        # keep only the tail after the last complete start code
        # (a NAL may still be mid-read; annexb_nals would split it wrongly)
        best = -1
        k = 0
        data = bytes(carry)
        while k < len(data):
            if data[k] != 0:
                k += 1
                continue
            j = k
            while j < len(data) and data[j] == 0:
                j += 1
            if j < len(data) and data[j] == 1:
                if j - k >= 2:  # valid start code needs at least 00 00 01
                    best = k
                k = j + 1
            else:
                k = max(k + 1, j)
        if best >= 0:
            # complete NALs end at best (start of the last one)
            complete = bytes(carry[:best])
            yield from annexb_nals(complete)
            del carry[:best]
        else:
            # no start code yet at all: still need a start code, keep everything
            if len(carry) > 8 * 1024 * 1024:
                # pathological: no start code seen; drop leading zeros only
                lead = 0
                while lead < len(carry) and carry[lead] == 0:
                    lead += 1
                del carry[:lead]


def mux_loop(enc: subprocess.Popen, clients: list, lock: threading.Lock, stop: threading.Event):
    """Assemble whole access units and emit CONFIG then picture packets."""
    config = bytearray()
    sent_first = False
    au = bytearray()
    au_has_vcl = False
    au_irap = False
    pts0 = time.time()

    def send(flags: int, payload: bytes):
        pts = int((time.time() - pts0) * 1_000_000)
        pkt = pack(flags, pts, payload)
        STATS["bytes"] += len(pkt)
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

    def flush_au():
        nonlocal au, au_has_vcl, au_irap, sent_first
        if not au_has_vcl:
            au = bytearray()
            au_irap = False
            return
        if not sent_first:
            if not au_irap or not config:
                # wait for a random-access picture with parameter sets available
                au = bytearray()
                au_has_vcl = False
                au_irap = False
                return
        if config and (not sent_first or au_irap):
            send(FLAG_CONFIG, bytes(config))
        flags = FLAG_FRAME | (FLAG_IDR if au_irap else 0)
        send(flags, bytes(au))
        sent_first = True
        au = bytearray()
        au_has_vcl = False
        au_irap = False

    for nal in iter_nals(enc.stdout):
        if stop.is_set():
            break
        t, first = nal_info(nal)
        if t == NAL_VPS:
            # new parameter-set group: keep only the latest VPS+SPS+PPS
            config = bytearray()
            config.extend(nal)
            continue
        if t in (NAL_SPS, NAL_PPS):
            config.extend(nal)
            continue
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
    flush_au()


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
        print("CLIENT", addr, flush=True)
        with lock:
            clients.append(conn)
            STATS["clients"] = len(clients)


def stderr_pump(enc: subprocess.Popen):
    for line in iter(enc.stderr.readline, b""):
        sys.stderr.buffer.write(b"FF " + line)
        sys.stderr.buffer.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=LISTEN_PORT)
    args = ap.parse_args()

    enc = start_encoder()
    stop = threading.Event()
    clients: list = []
    lock = threading.Lock()

    threading.Thread(target=render_loop, args=(enc, stop), daemon=True).start()
    threading.Thread(target=mux_loop, args=(enc, clients, lock, stop), daemon=True).start()
    threading.Thread(target=stderr_pump, args=(enc,), daemon=True).start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, args.port))
    srv.listen(4)
    print(f"LISTEN {LISTEN_HOST}:{args.port} {W}x{H}@{FPS} hevc_qsv main10 au-assembler", flush=True)
    threading.Thread(target=accept_loop, args=(srv, clients, lock, stop), daemon=True).start()

    try:
        while True:
            time.sleep(2)
            dt = max(0.001, time.time() - STATS["start"])
            mbps = STATS["bytes"] * 8 / dt / 1e6
            print(
                f"stat frames={STATS['frames']} idr={STATS['idr']} cfg={STATS['config']} "
                f"clients={STATS['clients']} render={STATS['render_ms']:.1f}ms "
                f"avg={mbps:.2f}Mbps",
                flush=True,
            )
            if enc.poll() is not None:
                print("encoder exited", enc.returncode, flush=True)
                break
    except KeyboardInterrupt:
        pass
    stop.set()
    try:
        srv.close()
    except Exception:
        pass
    try:
        enc.kill()
    except Exception:
        pass


if __name__ == "__main__":
    main()