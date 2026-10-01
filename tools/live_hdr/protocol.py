"""Toastovač live HDR framing. Version 2. Little-endian.

Packet (20 bytes, same layout as v1):
  magic[4] = b'TZHL'
  version u8 = 2
  flags u8
  stream u16   # STREAM_*  (v1 receivers treated this as reserved=0 / VIDEO)
  pts_us u64
  payload_len u32
  payload[payload_len]

v1 senders wrote reserved=0 which is STREAM_VIDEO, so a v2 receiver still
understands old video-only streams. v1 receivers that reject version!=1
need a matching Android update (this run).
"""
from __future__ import annotations

import struct

MAGIC = b"TZHL"
VERSION = 2
HEADER = struct.Struct("<4sBBHQI")  # 20 bytes

STREAM_VIDEO = 0
STREAM_AUDIO = 1
STREAM_CONTROL = 2

FLAG_CONFIG = 0x01
FLAG_IDR = 0x02
FLAG_FRAME = 0x04
FLAG_EOS = 0x08
FLAG_DISCONTINUITY = 0x10

# Audio config payload (little-endian):
#   u32 sample_rate
#   u16 channels
#   u16 bits          # 16
#   u32 bytes_per_sec
AUDIO_CONFIG = struct.Struct("<IHHI")


def pack(flags: int, pts_us: int, payload: bytes, stream: int = STREAM_VIDEO) -> bytes:
    return HEADER.pack(MAGIC, VERSION, flags, stream, pts_us, len(payload)) + payload


def unpack_header(buf: bytes):
    if len(buf) < HEADER.size:
        raise ValueError("short header")
    magic, ver, flags, stream, pts, plen = HEADER.unpack(buf[: HEADER.size])
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic!r}")
    if ver not in (1, VERSION):
        raise ValueError(f"bad version {ver}")
    return flags, pts, plen, stream
