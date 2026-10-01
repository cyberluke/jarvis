"""Micro-repro: host-like timing against a live broker.

Sequence mirrors EverywherePipeClient exactly:
  connect -> subscribe -> read ack -> sleep 20s -> ping -> read pong.
Run with the traced hold-broker running:
  python tools/test_broker_ping.py --client-only --timing
"""
from __future__ import annotations

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

import ctypes  # noqa: E402

INVALID = ctypes.c_void_p(-1).value
k32 = ctypes.windll.kernel32


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
            raise RuntimeError(f"write failed err={k32.GetLastError()} "
                               f"len={len(payload)}")

    def recv(self, timeout_ms: int = 8000):
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


def run() -> None:
    c = Client()
    print("connected")
    c.send({"protocol": "toustovac-everywhere/1", "kind": "subscribe",
            "request_id": "h00000000001"})
    ack = c.recv()
    print(f"ack kind={ack.get('kind') if ack else None}")
    print("sleeping 20s (host-like gap between ack and first ping)...")
    time.sleep(20)
    t0 = time.monotonic()
    c.send({"protocol": "toustovac-everywhere/1", "kind": "ping",
            "request_id": "h00000000002"})
    try:
        r = c.recv(10000)
        dt = (time.monotonic() - t0) * 1000
        print(f"pong {dt:.1f} ms kind={r.get('kind') if r else None}")
    except TimeoutError:
        print("PONG TIMEOUT (reproduced host wedge)")


if __name__ == "__main__":
    run()