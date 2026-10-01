#!/usr/bin/env python3
"""TEMP_DIAGNOSTIC_* — Annex-B NAL / AU structure inspector.

Reads a raw HEVC Annex-B elementary stream and prints a compact AU map:
for each access unit: index, PTS-like order, byte size, NAL type list,
flags for VPS/SPS/PPS/SEI/AUD/IDR/CRA and whether it contains a VCL slice.

Usage:
  python _au_inspect.py <file.h265> [--first N]
"""
from __future__ import annotations

import sys

NAL_TYPES = {
    0: "TRAIL_N", 1: "TRAIL_R", 2: "TSA_N", 3: "TSA_R", 4: "STSA_N",
    5: "STSA_R", 6: "RADL_N", 7: "RADL_R", 8: "RASL_N", 9: "RASL_R",
    10: "RSV_VCL_N10", 11: "RSV_VCL_R11", 12: "RSV_VCL_N12", 13: "RSV_VCL_R13",
    14: "RSV_VCL_N14", 15: "RSV_VCL_R15", 16: "BLA_W_LP", 17: "BLA_W_RADL",
    18: "BLA_N_LP", 19: "IDR_W_RADL", 20: "IDR_N_LP", 21: "CRA_NUT",
    22: "RSV_IRAP_VCL22", 23: "RSV_IRAP_VCL23", 24: "RSV_VCL24", 25: "RSV_VCL25",
    26: "RSV_VCL26", 27: "RSV_VCL27", 28: "RSV_VCL28", 29: "RSV_VCL29",
    30: "RSV_VCL30", 31: "RSV_VCL31", 32: "VPS", 33: "SPS", 34: "PPS",
    35: "AUD", 36: "EOS", 37: "EOB", 38: "FD", 39: "PREFIX_SEI", 40: "SUFFIX_SEI",
    41: "RSV_NVCL41", 42: "RSV_NVCL42", 43: "RSV_NVCL43", 44: "RSV_NVCL44",
    45: "RSV_NVCL45", 46: "RSV_NVCL46", 47: "RSV_NVCL47", 48: "UNSPEC48",
}

IRAP = {16, 17, 18, 19, 20, 21, 22, 23}
VCL = set(range(0, 32))


def scan_start_codes(buf: bytearray):
    """Yield (start, end) byte ranges of each NAL incl. its start code."""
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
    out = []
    for k, s0 in enumerate(starts):
        if k + 1 < len(starts):
            out.append((s0, starts[k + 1], s0 + (4 if buf[s0 + 3] == 1 else 3)))
        else:
            out.append((s0, n, s0 + (4 if n > s0 + 3 and buf[s0 + 3] == 1 else 3)))
    return out


def nal_type(hdr_off: int, buf: bytes) -> int:
    if hdr_off >= len(buf):
        return -1
    return (buf[hdr_off] >> 1) & 0x3F


def inspect(path: str, first: int = 40):
    data = open(path, "rb").read()
    buf = bytearray(data)
    nals = scan_start_codes(buf)
    print(f"FILE {path} total_bytes={len(data)} total_nals={len(nals)}")
    aus = []  # list of (start_nal_idx, end_nal_idx_excl, has_vcl, has_irap, has_param)
    cur = []
    for idx, (s0, s1, hdr) in enumerate(nals):
        t = nal_type(hdr, buf)
        is_vcl = t in VCL
        is_param = t in (32, 33, 34)
        is_sei = t in (39, 40)
        is_aud = t == 35
        # A new AU starts when: a VCL NAL appears and we already have VCL
        # (i.e. second picture) OR an IRAP/param set arrives after VCL seen.
        if is_vcl and any(x[0] in VCL for x in cur):
            aus.append(cur)
            cur = []
        elif cur and is_param and any(x[0] in VCL for x in cur):
            # parameter sets after a VCL belong to next AU
            aus.append(cur)
            cur = []
        elif cur and is_aud and any(x[0] in VCL for x in cur):
            aus.append(cur)
            cur = []
        cur.append((t, s1 - s0))
    if cur:
        aus.append(cur)
    print(f"AU_COUNT {len(aus)}")
    for k, au in enumerate(aus[:first]):
        types = [t for t, _ in au]
        names = [NAL_TYPES.get(t, f"?{t}") for t in types]
        nbytes = sum(b for _, b in au)
        has = {
            "VPS": 32 in types, "SPS": 33 in types, "PPS": 34 in types,
            "SEI": any(t in (39, 40) for t in types),
            "AUD": 35 in types,
            "IDR": any(t in (19, 20) for t in types),
            "CRA": 21 in types,
            "BLA": any(t in (16, 17, 18) for t in types),
            "VCL": any(t in VCL for t in types),
        }
        flags = ",".join(kk for kk, v in has.items() if v)
        print(f"AU {k:3d} bytes={nbytes:8d} nals={len(au):2d} "
              f"[{','.join(names[:12])}{'...' if len(names) > 12 else ''}] "
              f"{{{flags}}}")


if __name__ == "__main__":
    p = sys.argv[1]
    first = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    inspect(p, first)