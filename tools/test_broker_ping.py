"""Standalone Everywhere broker ping test (repro tool).

Mimics the native host's pipe client: connect, subscribe, then send a
sequence of pings and measure round-trip latency. If the broker answers
instantly here but not under the daemon, the wedge is daemon-process
contention; if it wedges here too, the broker's session loop is broken.

Usage: python tools/test_broker_ping.py [--count N]
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time

sys.path.insert(0, "src")

from jarvis.everywhere import broker as broker_mod  # noqa: E402
from jarvis.everywhere.protocol import PIPE_NAME, decode, encode  # noqa: E402

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
PIPE_READMODE_MESSAGE = 0x2
ERROR_MORE_DATA = 234
INVALID = ctypes.c_void_p(-1).value

k32 = ctypes.windll.kernel32


class Cfg:
    everywhere_pipe_name = ""
    everywhere_ocr_backend = ""


class Client:
    def __init__(self) -> None:
        self.h = k32.CreateFileW(PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0,
                                 None, OPEN_EXISTING, 0, None)
        if not self.h or self.h == INVALID:
            raise RuntimeError(f"connect failed err={k32.GetLastError()}")
        mode = ctypes.c_uint(PIPE_READMODE_MESSAGE)
        if not k32.SetNamedPipeHandleState(self.h, ctypes.byref(mode),
                                           None, None):
            raise RuntimeError(f"set mode failed err={k32.GetLastError()}")

    def send(self, obj: dict) -> None:
        payload = encode(obj)
        buf = ctypes.create_string_buffer(payload, len(payload))
        n = ctypes.c_ulong(0)
        ok = k32.WriteFile(self.h, buf, len(payload), ctypes.byref(n), None)
        if not ok:
            raise RuntimeError(f"write failed err={k32.GetLastError()}")

    def recv(self, timeout_ms: int = 8000):
        """Read one frame with a wall-clock cap (ReadFile blocks)."""
        out: list = []
        err: list = []

        def worker() -> None:
            try:
                buf = ctypes.create_string_buffer(8192)
                n = ctypes.c_ulong(0)
                data = bytearray()
                while True:
                    ok = k32.ReadFile(self.h, buf, 8192, ctypes.byref(n), None)
                    errcode = 0 if ok else k32.GetLastError()
                    if ok:
                        data += buf.raw[: int(n.value)]
                        if errcode != ERROR_MORE_DATA:
                            break
                    elif errcode == ERROR_MORE_DATA:
                        data += buf.raw[:8192]
                    else:
                        out.append(None)
                        return
                obj, _reason = decode(bytes(data))
                out.append(obj)
            except Exception as exc:  # pragma: no cover
                err.append(exc)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout_ms / 1000.0)
        if t.is_alive():
            raise TimeoutError("read timed out")
        if err:
            raise err[0]
        return out[0]


def run(count: int, client_only: bool = False) -> None:
    b = None
    if not client_only:
        cfg = Cfg()
        b = broker_mod.EverywhereBroker(cfg)
        b.start()
        time.sleep(0.4)
        print(f"broker listening: {PIPE_NAME}")
    c = Client()
    print("client connected")
    c.send({"protocol": "toustovac-everywhere/1", "kind": "subscribe",
            "request_id": "t00000000001"})
    reply = c.recv()
    print(f"subscribe reply kind={reply.get('kind') if reply else None}")
    failures = 0
    for i in range(count):
        rid = f"ping{i:04d}"
        t0 = time.monotonic()
        c.send({"protocol": "toustovac-everywhere/1", "kind": "ping",
                "request_id": rid})
        try:
            r = c.recv()
        except TimeoutError:
            failures += 1
            print(f"  ping {i}: TIMEOUT (>8s)")
            continue
        dt = (time.monotonic() - t0) * 1000
        ok_kind = bool(r and r.get("kind") == "pong")
        if not ok_kind:
            failures += 1
        print(f"  ping {i}: {dt:7.1f} ms kind={r.get('kind') if r else None}")
    print(f"RESULT: {count - failures}/{count} pongs ok")
    if b is not None:
        b.stop()


if __name__ == "__main__":
    argv = list(sys.argv[1:])
    count = int(argv[argv.index("--count") + 1]) \
        if "--count" in argv else 10
    run(count, client_only="--client-only" in argv)
