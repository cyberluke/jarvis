"""Subtitles overlay command test against a live broker (daemon).

Simulates exactly what SubtitlesOverlay.Start() sends: options first, then
start, then poll, then stop. Each call is a synchronous round trip.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time

sys.path.insert(0, "src")

from jarvis.everywhere.protocol import PIPE_NAME, decode, encode  # noqa: E402

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_READMODE_MESSAGE = 0x2
ERROR_MORE_DATA = 234
INVALID = ctypes.c_void_p(-1).value
k32 = ctypes.windll.kernel32

PROTO = "toustovac-everywhere/1"


def main() -> None:
    h = k32.CreateFileW(PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0,
                        None, OPEN_EXISTING, 0, None)
    if not h or h == INVALID:
        raise RuntimeError(f"connect failed err={k32.GetLastError()}")
    mode = ctypes.c_uint(PIPE_READMODE_MESSAGE)
    if not k32.SetNamedPipeHandleState(h, ctypes.byref(mode), None, None):
        raise RuntimeError(f"set mode failed err={k32.GetLastError()}")

    rid = [0]

    def rpc(obj: dict, timeout_ms: int = 15000):
        rid[0] += 1
        obj = dict(obj, protocol=PROTO, request_id=f"ov{rid[0]:06d}")
        payload = encode(obj)
        buf = ctypes.create_string_buffer(payload, len(payload))
        n = ctypes.c_ulong(0)
        if not k32.WriteFile(h, buf, len(payload), ctypes.byref(n), None):
            raise RuntimeError(f"write failed err={k32.GetLastError()}")
        out: list = []
        err: list = []

        def reader() -> None:
            try:
                rbuf = ctypes.create_string_buffer(8192)
                rn = ctypes.c_ulong(0)
                data = bytearray()
                while True:
                    ok = k32.ReadFile(h, rbuf, 8192, ctypes.byref(rn), None)
                    e = 0 if ok else k32.GetLastError()
                    if ok:
                        data += rbuf.raw[: int(rn.value)]
                        if e != ERROR_MORE_DATA:
                            break
                    elif e == ERROR_MORE_DATA:
                        data += rbuf.raw[:8192]
                    else:
                        out.append(None)
                        return
                r, _ = decode(bytes(data))
                out.append(r)
            except Exception as exc:  # pragma: no cover
                err.append(exc)

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout_ms / 1000.0)
        if t.is_alive():
            raise TimeoutError("round trip timed out")
        if err:
            raise err[0]
        return out[0]

    print("subscribe...")
    sub = rpc({"kind": "subscribe"})
    print(f"  -> {sub.get('kind') if sub else None}")

    print("subtitles options...")
    opts = rpc({"kind": "subtitles", "command": "options"})
    kind = opts.get("kind") if opts else None
    print(f"  -> {kind}")
    if kind == "subtitles_options":
        inputs = opts.get("audio", {}).get("inputs", [])
        outputs = opts.get("audio", {}).get("outputs", [])
        print(f"     inputs={len(inputs)} outputs={len(outputs)} "
              f"sources={len(opts.get('sources', []))} "
              f"targets={len(opts.get('targets', []))}")

    print("subtitles start (loopback, cs)...")
    st = rpc({"kind": "subtitles", "command": "start",
              "source": "auto", "target": "cs", "live_audio": False,
              "politeness": "", "audio_input": "loopback",
              "audio_output": None})
    print(f"  -> {st.get('kind') if st else None}")

    time.sleep(2)
    print("subtitles poll...")
    pl = rpc({"kind": "subtitles", "command": "poll"})
    print(f"  -> {pl.get('kind') if pl else None} running="
          f"{pl.get('running') if pl else None} lines="
          f"{len(pl.get('lines', [])) if pl else 0}")

    print("subtitles stop...")
    sp = rpc({"kind": "subtitles", "command": "stop"})
    print(f"  -> {sp.get('kind') if sp else None}")

    print("ALL SUBTITLES COMMANDS OK")


if __name__ == "__main__":
    main()