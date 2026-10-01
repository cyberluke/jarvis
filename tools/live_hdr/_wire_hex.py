#!/usr/bin/env python3
"""TEMP_DIAGNOSTIC_* — TCP sink with per-packet hexdump of first N packets."""
import socket
import struct
import sys
import time

MAGIC = b"TZHL"
HEADER = struct.Struct("<4sBBHQI")


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8768
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 12.0
    s = socket.create_connection((host, port), timeout=5)
    s.settimeout(1.0)
    t_end = time.time() + duration
    n = 0
    while time.time() < t_end:
        try:
            hdr = b""
            while len(hdr) < HEADER.size:
                c = s.recv(HEADER.size - len(hdr))
                if not c:
                    return
                hdr += c
            magic, ver, flags, _r, pts, plen = HEADER.unpack(hdr)
            payload = b""
            while len(payload) < plen:
                c = s.recv(plen - len(payload))
                if not c:
                    return
                payload += c
            n += 1
            if n <= 4:
                print(f"PKT {n} flags=0x{flags:02x} len={plen}")
                b = payload[:96]
                print(b.hex(" "))
                print(" ".join(f"{i:02d}" for i in range(len(b))))
        except socket.timeout:
            continue
    print("DONE pkts", n)


if __name__ == "__main__":
    main()