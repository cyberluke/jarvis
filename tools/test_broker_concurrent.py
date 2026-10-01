"""Exact host I/O structure repro: persistent reader thread + writer thread.

The native host runs a persistent ReadLoop (always one pending ReadFile on
the handle) while the WriteLoop writes pings from another thread. All my
previous Python clients started a fresh read thread only *after* sending,
so they never had a pending read while writing. This test mirrors the host.
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

READ_BUF = 8192  # broker-side read buffer size


def main() -> None:
    h = k32.CreateFileW(PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0,
                        None, OPEN_EXISTING, 0, None)
    if not h or h == INVALID:
        raise RuntimeError(f"connect failed err={k32.GetLastError()}")
    mode = ctypes.c_uint(PIPE_READMODE_MESSAGE)
    if not k32.SetNamedPipeHandleState(h, ctypes.byref(mode), None, None):
        raise RuntimeError(f"set mode failed err={k32.GetLastError()}")

    # ── persistent reader thread (mirrors the host's ReadLoop) ──
    received: list = []
    reader_stop = threading.Event()

    def reader() -> None:
        buf = ctypes.create_string_buffer(READ_BUF)
        n = ctypes.c_ulong(0)
        data = bytearray()
        while not reader_stop.is_set():
            ok = k32.ReadFile(h, buf, READ_BUF, ctypes.byref(n), None)
            err = 0 if ok else k32.GetLastError()
            if not ok:
                received.append(("ERR", err))
                return
            data += buf.raw[: int(n.value)]
            if err == ERROR_MORE_DATA:
                continue
            obj, _ = decode(bytes(data))
            data = bytearray()
            received.append(("MSG", obj.get("kind") if obj else None))
            # keep reading (persistent loop)

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()

    def send(obj: dict) -> None:
        payload = encode(obj)
        buf = ctypes.create_string_buffer(payload, len(payload))
        n = ctypes.c_ulong(0)
        t0 = time.monotonic()
        ok = k32.WriteFile(h, buf, len(payload), ctypes.byref(n), None)
        dt = (time.monotonic() - t0) * 1000
        print(f"  write {len(payload)}B ok={ok} took={dt:.1f}ms "
              f"err={0 if ok else k32.GetLastError()}")

    print("subscribe")
    send({"protocol": "toustovac-everywhere/1", "kind": "subscribe",
          "request_id": "r00000000001"})
    time.sleep(1)
    print("pings with persistent reader pending...")
    for i in range(5):
        send({"protocol": "toustovac-everywhere/1", "kind": "ping",
              "request_id": f"r0000000000{i + 2}"})
        time.sleep(1)
    time.sleep(1)
    print(f"received: {received}")
    reader_stop.set()


if __name__ == "__main__":
    main()