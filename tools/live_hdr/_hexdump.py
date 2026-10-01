#!/usr/bin/env python3
"""TEMP_DIAGNOSTIC_* — hexdump first packets from the wire capture."""
import struct

HEADER = struct.Struct("<4sBBHQI")
data = open(r"C:\Users\LUKES~1.COR\AppData\Local\Temp\kilo\wire_payload.bin", "rb").read()
off = 0
for k in range(6):
    print(f"=== payload chunk {k} (offset {off}) ===")
    print(data[off:off + 80].hex(" "))
    # show first 60 bytes as bytes with positions
    b = data[off:off + 60]
    print(" ".join(f"{i:02d}" for i in range(len(b))))
    off += 4096  # pretend step (just for display variety)