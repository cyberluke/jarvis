#!/usr/bin/env python3
"""TEMP_DIAGNOSTIC_* — TCP sink: record TZHL packets from the live server.

Saves packet log (idx, flags, pts, len, first-8 NAL types) + raw payload
stream to files for offline analysis. Connects to the running server.
"""
from __future__ import annotations

import socket
import struct
import sys

MAGIC = b"TZHL"
HEADER = struct.Struct("<4sBBHQI")
NAL_NAMES = {
    0: "TRAIL_N", 1: "TRAIL_R", 19: "IDR_W_RADL", 20: "IDR_N_LP",
    21: "CRA", 32: "VPS", 33: "SPS", 34: "PPS", 35: "AUD", 39: "PREFIX_SEI",
    40: "SUFFIX_SEI", 16: "BLA_W_LP", 17: "BLA_W_RADL", 18: "BLA_N_LP",
}


def nal_types(payload: bytes, maxn=10):
    out = []
    i = 0
    n = len(payload)
    while i < n and len(out) < maxn:
        if payload[i] != 0:
            i += 1
            continue
        j = i
        while j < n and payload[j] == 0:
            j += 1
        if j < n and payload[j] == 1:
            if j - i >= 2:  # valid start code needs at least 00 00 01
                hdr = j + 1
                if hdr < n:
                    t = (payload[hdr] >> 1) & 0x3F
                    out.append(NAL_NAMES.get(t, f"?{t}"))
            i = j + 1
        else:
            i = max(i + 1, j)
    return out


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8768
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 12.0
    logf = open(sys.argv[4] if len(sys.argv) > 4 else "wire_log.txt", "w")
    rawf = open(sys.argv[5] if len(sys.argv) > 5 else "wire_payload.bin", "wb")
    import time

    s = socket.create_connection((host, port), timeout=5)
    s.settimeout(1.0)
    print("CONNECTED", host, port, flush=True)
    t_end = time.time() + duration
    n = 0
    total = 0
    while time.time() < t_end:
        try:
            hdr = b""
            while len(hdr) < HEADER.size:
                c = s.recv(HEADER.size - len(hdr))
                if not c:
                    print("EOF", flush=True)
                    return
                hdr += c
            magic, ver, flags, _r, pts, plen = HEADER.unpack(hdr)
            if magic != MAGIC:
                print("BAD MAGIC", flush=True)
                return
            payload = b""
            while len(payload) < plen:
                c = s.recv(plen - len(payload))
                if not c:
                    print("EOF mid-payload", flush=True)
                    return
                payload += c
            n += 1
            total += len(payload)
            t = nal_types(payload)
            logf.write(f"PKT {n:4d} flags=0x{flags:02x} pts={pts} len={len(payload):7d} "
                       f"nals=[{','.join(t)}]\n")
            logf.flush()
            rawf.write(payload)
        except socket.timeout:
            continue
    logf.write(f"TOTAL pkts={n} payload_bytes={total}\n")
    logf.flush()
    print("DONE pkts", n, "payload_bytes", total, flush=True)


if __name__ == "__main__":
    main()