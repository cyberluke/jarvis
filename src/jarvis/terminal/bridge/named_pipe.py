"""Current-user named-pipe bridge server (Toustovac.TerminalBridge.v1).

Bounded multi-instance message-mode pipe via kernel32 (M3.2): a pool of
MAX_PIPE_INSTANCES=8 instances, one worker thread per instance. One
pipe name, N instances — Windows dispatches each connecting client to a
free instance, so the VSIX and PowerShell producers cannot starve each
other.

Connection lifetimes (proven from client sources):
  * VSIX (extension.ts pipeRoundTrip): net.connect → write one frame →
    read until first full decoded frame → sock.end(); destroyed on a
    1200 ms timer. One short-lived transaction per connection; the ack
    is a second, equally bounded round trip.
  * PowerShell (Toustovac.TerminalBridge.psm1 Send-ToustovacFrame):
    NamedPipeClientStream.Connect(50 ms) → write one frame → one Read
    → Dispose in finally. One transaction per prompt boundary; dropped
    frames bump $ToustovacDropped instead of blocking the prompt.

Boundary guarantees (§P1-2, proven from this source):
  * bounded pool (nMaxInstances=MAX_PIPE_INSTANCES, no
    PIPE_UNLIMITED_INSTANCES) → each instance serves one client at a
    time; bounded queue = per-session dict capped at _MAX_SESSIONS
    entries;
  * frame bounds: 4-byte length prefix, payload ≤ MAX_PAYLOAD_BYTES;
  * protocol id 'toustovac-terminal-bridge/2' enforced; unknown kinds
    rejected with a counted 'rejected' stat;
  * freshness: beacons older than _freshness are invisible to readers;
  * identity: (pid, process_created_ns) pair from the beacon is kept
    verbatim so the broker can PID-reuse-check;
  * action requests are answered on the SAME connected pipe instance
    with a strict deadline (no next-beacon coupling); the telemetry
    beacon itself never executes anything — the ack is a pure receipt.

Security note (M1.2): the Win32 message-mode pipe is created with an
explicit DACL whose only interactive trustee is the current process
token's numeric logon SID (S-1-5-5-<n>-<n>), read via
GetTokenInformation(TokenGroups) with the SE_GROUP_LOGON_ID mask test.
It is a local, same-logon channel — no TCP socket, no service. If the
SID cannot be derived the bridge fails closed (no pipe, no default
descriptor).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

from ...debug import debug_log
from .protocol import (
    FRAME_HEADER_BYTES,
    MAX_FRAME_BYTES,
    MAX_PAYLOAD_BYTES,
    PROTOCOL_ID,
    decode_frame,
    encode,
    project,
    validate_action,
    validate_beacon,
    _ACTION_FIELDS,
    _BEACON_FIELDS,
)

PIPE_NAME = r"\\.\pipe\Toustovac.TerminalBridge.v1"
# WinBase.h values (M5 probe-confirmed): nOpenMode = PIPE_ACCESS_DUPLEX,
# nPipeMode composes PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE |
# PIPE_REJECT_REMOTE_CLIENTS.
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_MESSAGE = 0x00000004
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
# M5.1 — every pool pipe is created for overlapped I/O; every
# ConnectNamedPipe/ReadFile/WriteFile then receives a live OVERLAPPED.
FILE_FLAG_OVERLAPPED = 0x40000000
# aclapi.h security-information flags for GetSecurityInfo.
DACL_SECURITY_INFORMATION = 0x00000004
OWNER_SECURITY_INFORMATION = 0x00000001
GROUP_SECURITY_INFORMATION = 0x00000002
SE_KERNEL_OBJECT = 0
ERROR_SUCCESS = 0
# M6 §2 — authoritative winerror.h values, defined ONCE here; every
# branch below uses these symbolic names (M5 values 532 / 234-as-no-data
# / 235 were wrong and are deleted).
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_MORE_DATA = 234
WAIT_TIMEOUT = 258
ERROR_PIPE_CONNECTED = 535
ERROR_PIPE_LISTENING = 536
ERROR_OPERATION_ABORTED = 995
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING = 997
ERROR_NOT_FOUND = 1168
WAIT_OBJECT_0 = 0
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
# aclapi.h ACL_INFORMATION_CLASS: 2 = AclSizeInformation
AclSizeInformation = 2
# WinNT.h ACE type numeric values (binary inspection authority; probe
# on a real ACL shows the first byte of an SDDL 'A' entry is 0x00):
# ACCESS_ALLOWED_ACE_TYPE = 0x00, ACCESS_DENIED_ACE_TYPE = 0x01.
ACCESS_ALLOWED_ACE_TYPE = 0
ACCESS_DENIED_ACE_TYPE = 1
# WinNT.h: SE_DACL_PROTECTED = 0x1000 (0x0010 is SE_SACL_PRESENT)
SE_DACL_PROTECTED = 0x1000
# M3.2 — bounded instance pool (no PIPE_UNLIMITED_INSTANCES). All
# instances share pipe type, access mode, buffers, timeout and DACL as
# required by CreateNamedPipeW.
MAX_PIPE_INSTANCES = 8
_JOIN_DEADLINE_SEC = 1.0
_MAX_SESSIONS = 32
_PUMP_TICK_SEC = 0.05
_READ_BUF = 8192
# M6 §3 — separate deadline domains (ms), validated once at import.
# The idle accept uses INFINITE instead (M6 §3.2), so no fixed
# per-operation wait constant exists anymore.
TERMINAL_BRIDGE_READ_TIMEOUT_MS = 1500
TERMINAL_BRIDGE_WRITE_TIMEOUT_MS = 1500
TERMINAL_BRIDGE_CANCEL_DRAIN_MS = 750
for _n, _v in (
        ("terminal_bridge_read_timeout_ms",
         TERMINAL_BRIDGE_READ_TIMEOUT_MS),
        ("terminal_bridge_write_timeout_ms",
         TERMINAL_BRIDGE_WRITE_TIMEOUT_MS),
        ("terminal_bridge_cancel_drain_ms",
         TERMINAL_BRIDGE_CANCEL_DRAIN_MS)):
    if not isinstance(_v, int) or _v <= 0:
        raise ValueError(f"invalid {_n}: {_v!r}")


def _remaining_ms(deadline: float) -> int:
    """Ceiling of remaining ms until an absolute monotonic deadline.
    Computed per chunk, never reset (M6 §3.3)."""
    rem = (deadline - time.monotonic()) * 1000.0
    return max(0, int(math.ceil(rem)))


def _drain_deadline(op_deadline: float) -> float:
    """Bounded cancellation-drain deadline, never later than the
    transaction deadline (M6 §3.4)."""
    return min(op_deadline,
               time.monotonic() + TERMINAL_BRIDGE_CANCEL_DRAIN_MS / 1000.0)

# Explicit SDDL (M1.2) built from the *real* token logon SID.
# SDDL alias facts (Microsoft docs): LG = Local Guest; the per-logon
# SID is the token group of form S-1-5-5-<decimal>-<decimal> flagged
# with the SE_GROUP_LOGON_ID attribute mask (0xC0000000), a flag mask,
# not a SID alias.
# Numeric grant mask 0xC0000000 = GENERIC_READ|GENERIC_WRITE, the exact
# access both real clients request (libuv named-pipe connect in Node,
# and .NET NamedPipeClientStream default ReadWrite). On pipe objects
# the generic-write expansion carries the shared bit 0x4
# (FILE_APPEND_DATA == FILE_CREATE_PIPE_INSTANCE == 0x4; 0x2 is
# FILE_WRITE_DATA) which every client's write needs; generic-read
# expands to FILE_READ_DATA|READ_EA|READ_ATTRIBUTES|SYNCHRONIZE. No SY
# ACE by habit: assistant, VSIX and PowerShell bridge all run in the
# interactive logon.
# "0x" prefix required: bare hex letters are rejected by the SDDL
# converter (GetLastError 1336 = ERROR_INVALID_SI).
_ACCESS_MASK = "0xC0000000"
_ACCESS_RIGHTS = ("GENERIC_READ", "GENERIC_WRITE")
_FORBIDDEN_TRUSTEES = ("LG", "IU", "AU", "NU", "WD")
# M3.1 truth table (file access-rights constants): the shared append /
# create-instance bit is 0x4 — FILE_APPEND_DATA ==
# FILE_CREATE_PIPE_INSTANCE == 0x00000004 (the M2 claim of 0x2 was
# wrong; 0x2 is FILE_WRITE_DATA). Derived below from the mapped mask,
# never hard-coded, so the report follows the live ACE.
FILE_CREATE_PIPE_INSTANCE_ALIAS_PRESENT = True
ACCESS_POLICY = "same_logon_generic_rw"
# Canonical named-pipe / file access-mask values (M3.1 §2.1):
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_CREATE_PIPE_INSTANCE = 0x00000004
FILE_READ_EA = 0x00000008
FILE_WRITE_EA = 0x00000010
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
READ_CONTROL = 0x00020000
SYNCHRONIZE = 0x00100000
GENERIC_READ_BIT = 0x80000000
GENERIC_WRITE_BIT = 0x40000000
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
# M4 §1: SDDL FX / FILE_GENERIC_EXECUTE is 0x001200A0 (STANDARD_RIGHTS_
# READ bits | EXECUTE 0x20 | READ_ATTRIBUTES | SYNCHRONIZE), not 0xA0.
FILE_GENERIC_EXECUTE = 0x001200A0
FILE_ALL_ACCESS = 0x001F01FF


def _map_generic_mask(adv, mask: int) -> int:
    """Expand generic bits via the Win32 MapGenericMask API (M4 §3.1).

    Canonical GENERIC_MAPPING used by the live report:
      GenericRead    = FILE_GENERIC_READ     = 0x00120089
      GenericWrite   = FILE_GENERIC_WRITE    = 0x00120116
      GenericExecute = FILE_GENERIC_EXECUTE  = 0x001200A0
      GenericAll     = FILE_ALL_ACCESS       = 0x001F01FF

    The raw ACE mask stays authoritative; the mapped value is a copy
    for effective-rights diagnostics only (never hides a raw mismatch).
    """
    gm = _GENERIC_MAPPING(FILE_GENERIC_READ, FILE_GENERIC_WRITE,
                          FILE_GENERIC_EXECUTE, FILE_ALL_ACCESS)
    m = _ct.c_uint32(mask & 0xFFFFFFFF)
    adv.MapGenericMask.restype = None
    adv.MapGenericMask.argtypes = [_ct.POINTER(_ct.c_uint32),
                                   _ct.POINTER(_GENERIC_MAPPING)]
    adv.MapGenericMask(_ct.byref(m), _ct.byref(gm))
    return int(m.value)
# WinNT.h: TOKEN_QUERY = 0x0008 (0x0002 is TOKEN_DUPLICATE and yields
# ERROR_ACCESS_DENIED on the subsequent GetTokenInformation call).
_TOKEN_QUERY = 0x00000008
_TOKEN_GROUPS = 2
_SE_GROUP_LOGON_ID = 0xC0000000
_LOGON_SID_RE = re.compile(r"^S-1-5-5-\d+-\d+$")
# group 2 = any non-empty mask token (numeric 0x… as configured, or
# normalized symbolic form such as "GWGR" as read back).
_ACE_RE = re.compile(r"\(([A-Z]);;([^;]+);;;([^)]+)\)")

# SDDL access-right tokens (MS-DTYP 2.4.2). Two-char "G?/A?" codes are
# the form returned by ConvertSecurityDescriptorToStringSecurityDescriptorW
# (observed: 0xC0000000 normalizes to "GWGR"). M2.3 fix: the legacy
# one-char codes are the OBJECT_INHERIT/INHERIT_ONLY flags C/I and the
# numeric-verified per-object rights; CC is ADS_RIGHT_DS_CREATE_CHILD
# (0x1), NOT 0x10000000 — 0x10000000 belongs to GA (GENERIC_ALL).
# Unknown codes yield None (never an invented mask).
_GENERIC_RIGHTS = {"GR": 0x80000000, "GW": 0x40000000,
                   "GX": 0x20000000, "GA": 0x10000000}
# Per-object / named SDDL rights (MS-DTYP 2.4.2 / WinNT.h), used for
# DIAGNOSTIC-ONLY SDDL normalization; the binary live-ACE mask is the
# authority (M3.1 §2.2). Corrected values (M3.1): FW = FILE_GENERIC_WRITE
# = 0x00120116 (STANDARD_RIGHTS_READ bits | WRITE_DATA|APPEND|WRITE_EA|
# WRITE_ATTRIBUTES|SYNCHRONIZE); FR = 0x00120089. GW remains the generic
# write bit 0x40000000. Anything else yields unknown (None).
_NAMED_RIGHTS = {
    "GA": 0x10000000,  # GENERIC_ALL
    "GR": 0x80000000,  # GENERIC_READ
    "GW": 0x40000000,  # GENERIC_WRITE (generic bit, not the FW map)
    "GX": 0x20000000,  # GENERIC_EXECUTE
    "RC": 0x00020000,  # READ_CONTROL
    "CC": 0x00000001,  # ADS_RIGHT_DS_CREATE_CHILD (NOT 0x10000000)
    "CT": 0x00000002,  # ADS_RIGHT_DS_DELETE_CHILD
    "EX": 0x00000004,  # ADS_RIGHT_DS_EXECUTE
    "FR": 0x00120089,  # FILE_GENERIC_READ
    "FW": 0x00120116,  # FILE_GENERIC_WRITE
    "FX": 0x001200A0,  # FILE_GENERIC_EXECUTE (M4: incl. 0x120000 bits)
    "FA": 0x001F01FF,  # FILE_ALL_ACCESS
}
# Precedence: numeric 0x token first, then two-char named codes composed
# by OR; unknown codes yield None (no invented masks).
_SYMBOLIC_RIGHTS: Dict[str, int] = {}

# M5 §5 — allocation ownership ledger. Diagnostic consistency counters
# only (not a garbage collector): every top-level LocalAlloc'd buffer
# and Win32 handle has exactly one owner path and one release, and the
# counters must return to balance. Borrowed pointers (DACL/ACE/SID inside
# a top-level descriptor) are NEVER freed independently.
_LEDGER: Dict[str, int] = {
    "configured_sd_allocated": 0, "configured_sd_freed": 0,
    "live_sd_allocated": 0, "live_sd_freed": 0,
    "sddl_string_allocated": 0, "sddl_string_freed": 0,
    "sid_string_allocated": 0, "sid_string_freed": 0,
    "pipe_handles_open": 0, "pipe_handles_closed": 0,
    "event_handles_open": 0, "event_handles_closed": 0,
}


def _client_role(obj: dict) -> str:
    """Privacy-safe producer role (M3.2 §3.3). `unknown` gains no extra
    capability — it only labels a counter."""
    kind = obj.get("kind")
    tp = str(obj.get("term_program") or "")
    if tp.startswith("vscode") or kind == "insert_ack":
        return "vscode"  # only the VSIX sets term_program / sends acks
    if "ps_edition" in obj or "execution_kind" in obj:
        return "powershell"
    if kind == "execution_record" and "output_tail" in obj:
        return "vscode"
    return "unknown"


def _mask_to_int(token: str) -> Optional[int]:
    """Parse a numeric or symbolic SDDL access mask into an int.

    Diagnostic-only helper (M3.1 §2.2): the binary live ACE mask is the
    authority. Generic codes come from _GENERIC_RIGHTS, named per-object
    rights from _NAMED_RIGHTS (CC=0x1, GA=0x10000000, FW=0x00120116,
    FR=0x00120089, ...); unknown codes yield None. Composed by OR; never
    an invented mask.
    """
    t = token.strip().upper()
    if not t:
        return None
    if t.startswith("0X"):
        try:
            return int(t, 16)
        except ValueError:
            return None
    total = 0
    i = 0
    while i < len(t):
        pair = t[i:i + 2]
        bit = _GENERIC_RIGHTS.get(pair)
        if bit is None:
            bit = _NAMED_RIGHTS.get(pair)
        if bit is None:
            bit = _SYMBOLIC_RIGHTS.get(pair)
        if bit is None:
            return None
        total |= bit
        i += 2
    return total


import ctypes as _ct  # noqa: E402


class _SID_AND_ATTRIBUTES(_ct.Structure):
    _fields_ = [("Sid", _ct.c_void_p), ("Attributes", _ct.c_ulong)]


class _GENERIC_MAPPING(_ct.Structure):
    """WinNT.h GENERIC_MAPPING — canonical file/pipe mapping (M4 §3.1):
    GenericRead 0x00120089, GenericWrite 0x00120116,
    GenericExecute 0x001200A0, GenericAll 0x001F01FF."""
    _fields_ = [("GenericRead", _ct.c_uint32),
                ("GenericWrite", _ct.c_uint32),
                ("GenericExecute", _ct.c_uint32),
                ("GenericAll", _ct.c_uint32)]


class ACE_HEADER(_ct.Structure):
    """WinNT.h ACE_HEADER — DWORD-aligned prefix. sizeof == 4."""
    _fields_ = [("AceType", _ct.c_ubyte),
                ("AceFlags", _ct.c_ubyte),
                ("AceSize", _ct.c_ushort)]


class ACCESS_ALLOWED_ACE(_ct.Structure):
    """WinNT.h ACCESS_ALLOWED_ACE. Mask.offset == 4,
    SidStart.offset == 8, sizeof == 12. The trustee SID begins at
    SidStart.offset; ACE_HEADER.AceSize is authoritative for length."""
    _fields_ = [("Header", ACE_HEADER),
                ("Mask", _ct.c_uint32),
                ("SidStart", _ct.c_uint32)]


class _OVERLAPPED(_ct.Structure):
    """WinBase.h OVERLAPPED with pointer-width-correct fields (M5 §2.2).

    Internal/InternalHigh are ULONG_PTR (ctypes.c_size_t), not fixed
    32-bit integers, so the layout is correct on Windows x64:
    sizeof == 32, Offset @ 16, OffsetHigh @ 20, hEvent @ 24.
    The structure must stay alive and unmodified until its operation
    reaches a terminal completion (GetOverlappedResult)."""
    _fields_ = [("Internal", _ct.c_size_t),
                ("InternalHigh", _ct.c_size_t),
                ("Offset", _ct.c_uint32),
                ("OffsetHigh", _ct.c_uint32),
                ("hEvent", _ct.c_void_p)]


def _sid_hash(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return hashlib.sha256(value.encode("ascii")).hexdigest()[:16]


def _query_logon_sid_ptr(k32, adv) -> Tuple[Optional[int], Optional[int]]:
    """M6 — the token logon SID as a live (ptr, token_handle) pair.

    Returns (sid_ptr, token_handle) — both integers, or (None, token) —
    so EqualSid can compare the borrowed trustee pointer with the real
    token SID pointer without CreateSid round-trips. The caller must
    CloseHandle the token exactly once.
    """
    import ctypes as ct
    k32.GetCurrentProcess.restype = ct.c_long
    k32.GetCurrentProcess.argtypes = []
    proc = k32.GetCurrentProcess()
    k32.OpenProcessToken.restype = ct.c_bool
    k32.OpenProcessToken.argtypes = [ct.c_void_p, ct.c_ulong,
                                     ct.POINTER(ct.c_void_p)]
    token = ct.c_void_p()
    if not k32.OpenProcessToken(proc, _TOKEN_QUERY, ct.byref(token)) \
            or not token.value:
        return None, None
    adv.GetTokenInformation.restype = ct.c_bool
    adv.GetTokenInformation.argtypes = [ct.c_void_p, ct.c_int,
                                        ct.c_void_p, ct.c_ulong,
                                        ct.POINTER(ct.c_ulong)]
    needed = ct.c_ulong(0)
    adv.GetTokenInformation(token.value, _TOKEN_GROUPS, None, 0,
                            ct.byref(needed))
    if not needed.value:
        return None, token.value
    blob = ct.create_string_buffer(needed.value)
    if not adv.GetTokenInformation(token.value, _TOKEN_GROUPS, blob,
                                   needed.value, ct.byref(needed)) \
            or not needed.value:
        return None, token.value
    addr = ct.addressof(blob)
    count = ct.c_uint.from_address(addr).value
    entry_off = 8 if ct.sizeof(ct.c_void_p) == 8 else 4
    stride = ct.sizeof(_SID_AND_ATTRIBUTES)
    matches = []
    for i in range(count):
        entry = _SID_AND_ATTRIBUTES.from_address(addr + entry_off
                                                 + i * stride)
        if (entry.Attributes & _SE_GROUP_LOGON_ID) == _SE_GROUP_LOGON_ID:
            matches.append(entry.Sid)
    if len(matches) != 1:
        return None, token.value
    return int(matches[0]), token.value


def _query_logon_sid(k32, adv) -> Optional[str]:
    """M1.2 — derive the numeric logon SID from this process's token.

    Fail-closed: returns None on any conversion/query failure or when
    zero/multiple token groups carry SE_GROUP_LOGON_ID.
    """
    import ctypes as ct
    # GetCurrentProcess: pseudo-handle -1 (keep default int restype so
    # ctypes sign-extends it correctly into c_void_p below).
    k32.GetCurrentProcess.restype = ct.c_long
    k32.GetCurrentProcess.argtypes = []
    proc = k32.GetCurrentProcess()
    k32.OpenProcessToken.restype = ct.c_bool
    k32.OpenProcessToken.argtypes = [ct.c_void_p, ct.c_ulong,
                                     ct.POINTER(ct.c_void_p)]
    token = ct.c_void_p()
    if not k32.OpenProcessToken(proc, _TOKEN_QUERY, ct.byref(token)) \
            or not token.value:
        return None
    try:
        adv.GetTokenInformation.restype = ct.c_bool
        adv.GetTokenInformation.argtypes = [ct.c_void_p, ct.c_int,
                                            ct.c_void_p, ct.c_ulong,
                                            ct.POINTER(ct.c_ulong)]
        needed = ct.c_ulong(0)
        # size probe with NULL buffer returns FALSE by design and only
        # fills the required size — the size is what matters here.
        adv.GetTokenInformation(token.value, _TOKEN_GROUPS, None, 0,
                                ct.byref(needed))
        if not needed.value:
            return None
        blob = ct.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token.value, _TOKEN_GROUPS, blob,
                                       needed.value, ct.byref(needed)) \
                or not needed.value:
            return None
        addr = ct.addressof(blob)
        count = ct.c_uint.from_address(addr).value
        # TOKEN_GROUPS: UINT GroupCount, then pointer-aligned entries
        entry_off = 8 if ct.sizeof(ct.c_void_p) == 8 else 4
        stride = ct.sizeof(_SID_AND_ATTRIBUTES)
        matches = []
        for i in range(count):
            entry = _SID_AND_ATTRIBUTES.from_address(addr + entry_off
                                                     + i * stride)
            if (entry.Attributes & _SE_GROUP_LOGON_ID) == _SE_GROUP_LOGON_ID:
                matches.append(entry.Sid)
        if len(matches) != 1:
            return None
        adv.IsValidSid.restype = ct.c_bool
        adv.IsValidSid.argtypes = [ct.c_void_p]
        if not adv.IsValidSid(matches[0]):
            return None
        adv.ConvertSidToStringSidW.restype = ct.c_ulong
        # W-API buffer is UTF-16: marshal as c_wchar_p, not c_char_p
        adv.ConvertSidToStringSidW.argtypes = [ct.c_void_p,
                                               ct.POINTER(ct.c_wchar_p)]
        out = ct.c_wchar_p()
        if not adv.ConvertSidToStringSidW(matches[0], ct.byref(out)) \
                or not out.value:
            return None
        sid = str(out.value)
        # Win32-owned buffer from ConvertSidToStringSidW is LocalAlloc'd
        # — the only top-level allocation of this call path (§5 ledger)
        _LEDGER["sid_string_allocated"] += 1
        k32.LocalFree.restype = ct.c_void_p
        k32.LocalFree.argtypes = [ct.c_void_p]
        k32.LocalFree(ct.cast(out, ct.c_void_p).value)
        _LEDGER["sid_string_freed"] += 1
        if not _LOGON_SID_RE.match(sid):
            return None
        return sid
    finally:
        k32.CloseHandle.restype = ct.c_bool
        k32.CloseHandle.argtypes = [ct.c_void_p]
        k32.CloseHandle(token)


class BridgeServer:
    """Message-mode pipe server: telemetry store + immediate action ack."""

    def __init__(self, pipe_name: str = PIPE_NAME,
                 freshness_sec: float = 30.0) -> None:
        import ctypes
        self._ct = ctypes
        self._k32 = ctypes.windll.kernel32
        self._adv = ctypes.windll.advapi32
        # Signatures set once here (the W APIs need LPCWSTR; ctypes maps
        # python str to ANSI otherwise — that mismatch made the earlier
        # conversion calls return "none").
        ct = ctypes
        adv = self._adv
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ct.c_wchar_p, ct.c_uint,
            ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_ulong),
        ]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            ct.c_long)
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ct.c_void_p, ct.c_uint, ct.c_uint,
            ct.POINTER(ct.c_wchar_p), ct.POINTER(ct.c_ulong),
        ]
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
            ct.c_long)
        self._pipe_name = pipe_name
        self.acl_sddl = ""
        self._logon_sid: Optional[str] = None
        self._configured_sddl = ""
        self._live_sddl: Optional[str] = None
        self._live_query_error: Optional[int] = None
        self._remote_rejected = False
        self._freshness = float(freshness_sec)
        self._lock = threading.Lock()
        self.beacons: Dict[str, Tuple[dict, float]] = {}
        self.executions: Dict[str, Tuple[dict, float]] = {}
        #: action frames awaiting pickup by the extension (per session)
        self.actions: Dict[str, Tuple[dict, float]] = {}
        self.nonce = secrets.token_hex(8)
        self.stats: Dict[str, int] = {
            "connected": 0, "disconnected": 0, "errors": 0,
            "rejected": 0, "beacons": 0, "executions": 0,
            "actions_served": 0, "actions_expired": 0,
            # M2.4 counter taxonomy (independent reasons, no silent merge)
            "frame_truncated": 0, "frame_oversized_advertised": 0,
            "frame_oversized_actual": 0, "frame_length_mismatch": 0,
            "frame_extra_bytes": 0, "frame_invalid_utf8": 0,
            "frame_invalid_json": 0, "frame_timeout": 0,
            # M3.2 §3.3 — same event counted under the §3.3 name too
            "frame_timeouts": 0,
            "frame_accepted": 0,
            # M3.2 §3.3 privacy-safe concurrency telemetry (no bodies,
            # no full SIDs: roles are the only labels)
            "configured_instances": MAX_PIPE_INSTANCES,
            "active_instances": 0,
            "peak_active_instances": 0,
            "accepted_connections": 0,
            "connect_timeouts": 0,
            "busy_rejections": 0,
            "idle_disconnects": 0,
            "graceful_disconnects": 0,
            "forced_disconnects": 0,
            "shutdown_incomplete": 0,
            # M5 §3.2 / M6 §7 connect + message-state counters
            "connect_immediate": 0, "connect_pending": 0,
            "connect_preconnected": 0, "connect_canceled": 0,
            "connect_failures": 0, "operation_timeouts": 0,
            "write_partial": 0, "delivery_unknown": 0,
            "read_more_data": 0, "peer_closing": 0,
            "peer_not_connected": 0, "cancel_drain_timeout": 0,
            "operation_canceled": 0, "delivered": 0,
        }
        #: per-role transaction counters (§3.3): unknown gains no capability
        self.client_role_counts: Dict[str, int] = {
            "vscode": 0, "powershell": 0, "unknown": 0,
        }
        # M3.1 §2.2 / M4 — per-instance binary live-ACE results, keyed
        # by pool generation and instance index (SDDL stays
        # diagnostic_only). One state per instance; the aggregate is
        # derived from the list, never from the latest handle alone.
        self._pool_generation: int = 0
        self._instance_results: list = []
        self._live_ace_mask: Optional[int] = None
        self._live_mapped_mask: Optional[int] = None
        self._live_trustee_sid: Optional[str] = None
        self._live_ace_count: Optional[int] = None
        self._live_unexpected_ace_count: int = 0
        self._live_protected: Optional[bool] = None
        self._failure_reason: str = ""
        self._verification_state: str = "not_created"
        self._thread: Optional[threading.Thread] = None
        self._threads: list = []
        self._handles: list = []
        self._stop = threading.Event()
        # M5 §4.3 — single documented ownership lock for every
        # close/cancel/disconnect transition on the owner records; the
        # telemetry lock (_lock) stays separate so I/O never serializes
        # behind report formatting.
        self._own_lock = threading.Lock()
        #: one owner record per pool instance (M5 §4.3 fields)
        self._records: list = []
        #: shared manual-reset Win32 shutdown event for the generation
        self._shutdown_evt: Optional[int] = None
        self._lifecycle_state: str = "stopped"

    # ── lifecycle ─────────────────────────────────────────────────────
    def _bind_io_apis(self) -> None:
        """M5 §2.2 — one explicit binding pass for the overlapped APIs.
        CancelSynchronousIo is deliberately NOT bound (its thread-handle
        contract does not match the per-OVERLAPPED CancelIoEx lifecycle)."""
        ct = self._ct
        k32 = self._k32
        k32.CreateEventW.restype = ct.c_void_p
        k32.CreateEventW.argtypes = [ct.c_void_p, ct.c_bool, ct.c_bool,
                                     ct.c_wchar_p]
        k32.SetEvent.restype = ct.c_bool
        k32.SetEvent.argtypes = [ct.c_void_p]
        k32.ResetEvent.restype = ct.c_bool
        k32.ResetEvent.argtypes = [ct.c_void_p]
        k32.WaitForMultipleObjects.restype = ct.c_ulong
        k32.WaitForMultipleObjects.argtypes = [
            ct.c_ulong, ct.POINTER(ct.c_void_p), ct.c_bool, ct.c_ulong]
        k32.ConnectNamedPipe.restype = ct.c_bool
        k32.ConnectNamedPipe.argtypes = [ct.c_void_p,
                                         ct.POINTER(_OVERLAPPED)]
        k32.ReadFile.restype = ct.c_bool
        k32.ReadFile.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_ulong,
                                 ct.POINTER(ct.c_ulong),
                                 ct.POINTER(_OVERLAPPED)]
        k32.WriteFile.restype = ct.c_bool
        k32.WriteFile.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_ulong,
                                  ct.POINTER(ct.c_ulong),
                                  ct.POINTER(_OVERLAPPED)]
        k32.GetOverlappedResult.restype = ct.c_ulong
        k32.GetOverlappedResult.argtypes = [
            ct.c_void_p, ct.POINTER(_OVERLAPPED), ct.POINTER(ct.c_ulong),
            ct.c_bool]
        k32.CancelIoEx.restype = ct.c_bool
        k32.CancelIoEx.argtypes = [ct.c_void_p, ct.POINTER(_OVERLAPPED)]
        k32.DisconnectNamedPipe.restype = ct.c_bool
        k32.DisconnectNamedPipe.argtypes = [ct.c_void_p]
        k32.CloseHandle.restype = ct.c_bool
        k32.CloseHandle.argtypes = [ct.c_void_p]
        k32.GetLastError.restype = ct.c_ulong
        k32.GetLastError.argtypes = []
        k32.LocalFree.restype = ct.c_void_p
        k32.LocalFree.argtypes = [ct.c_void_p]

    def start(self) -> None:
        if self._thread is not None:
            return
        self._bind_io_apis()
        if not self._shutdown_evt:
            # one shared manual-reset shutdown event for this pool
            # generation (M5 §2.2)
            self._shutdown_evt = self._k32.CreateEventW(
                None, True, False, None)
            _LEDGER["event_handles_open"] += 1
        self._lifecycle_state = "starting"
        self._thread = threading.Thread(
            target=self._serve, name="toustovac-pipe", daemon=True
        )
        self._thread.start()
        debug_log(
            f"bridge pipe listening: {self._pipe_name} "
            f"(instances={MAX_PIPE_INSTANCES}, overlapped=True)",
            "terminal")

    def stop(self) -> None:
        """M5 §4.2 / M6 §3.4 — deterministic overlapped shutdown order:
        1) mark generation stopping (no new handler work);
        2) signal the shared Win32 shutdown event exactly once;
        3) nudge each still-pending operation via
           CancelIoEx(pipe_handle, &current OVERLAPPED);
        4) workers drain terminal completions against their bounded
           drain deadlines and leave their waits;
        5) join all workers against ONE absolute one-second deadline
           (one deadline for all workers, not one per worker);
        6) for joined workers close operation event + pipe handle once;
        7) late workers or records whose cancellation drain stayed at
           ERROR_IO_INCOMPLETE keep their ownership for controlled late
           cleanup (never close a handle/event still referenced);
        8) close the shared shutdown event only after every worker ends.
        (CancelSynchronousIo is not used anywhere: it takes a thread
        handle and does not wait for the operation to complete.)"""
        k32 = self._k32
        ct = self._ct
        with self._own_lock:
            if self._lifecycle_state == "stopped":
                return
            self._lifecycle_state = "stopping"
        self._stop.set()
        if self._shutdown_evt:
            k32.SetEvent(self._shutdown_evt)
        for rec in list(self._records):
            if rec.get("operation_pending"):
                k32.CancelIoEx(rec["pipe_handle"],
                               ct.byref(rec["overlapped"]))
        deadline = time.monotonic() + _JOIN_DEADLINE_SEC
        remaining: list = []
        for i, t in enumerate(list(self._threads)):
            t.join(timeout=max(0.0, deadline - time.monotonic()))
            alive = t.is_alive()
            rec = self._records[i] if i < len(self._records) else None
            if rec is not None:
                with self._own_lock:
                    rec["worker_alive"] = not alive
            if alive or rec is None:
                remaining.append(i)
                continue
            if rec.get("drain_incomplete"):
                # M6 §3.4: drain stayed at ERROR_IO_INCOMPLETE at its
                # bounded deadline — retain ownership for late cleanup.
                remaining.append(i)
                continue
            if rec["operation_event"] and not rec["event_closed"]:
                k32.CloseHandle(rec["operation_event"])
                with self._own_lock:
                    rec["event_closed"] = True
                _LEDGER["event_handles_closed"] += 1
            if rec["pipe_handle"] and not rec["pipe_closed"]:
                try:
                    k32.DisconnectNamedPipe(rec["pipe_handle"])
                except Exception:
                    pass
                k32.CloseHandle(rec["pipe_handle"])
                with self._own_lock:
                    rec["pipe_closed"] = True
                _LEDGER["pipe_handles_closed"] += 1
        for _i in remaining:
            self.stats["shutdown_incomplete"] += 1
        if not remaining:
            # every worker terminated: safe to close the shared event
            self.stats["active_instances"] = 0
            self._verification_state = "shutdown"
            self._lifecycle_state = "stopped"
            if self._shutdown_evt:
                k32.CloseHandle(self._shutdown_evt)
                _LEDGER["event_handles_closed"] += 1
                self._shutdown_evt = None
            # sweep: record-owned handles close via their flags; raw
            # record-less handles (early fail-closed generations) close
            # through the same single-owner path
            self._close_handles_once()
        else:
            self._verification_state = "shutdown_incomplete"
            states = [(i, self._records[i].get("operation_kind"))
                      for i in remaining if i < len(self._records)]
            debug_log(
                f"bridge shutdown incomplete workers={states}",
                "terminal")

    # ── server loop (bounded pool, one worker per instance) ─────────
    def _serve(self) -> None:
        ct = self._ct
        k32 = self._k32
        # M0.2 — explicit security descriptor; fail closed without it.
        # Explicit signatures first: the W APIs need LPCWSTR, but ctypes
        # marshals python str as ANSI unless argtypes declare c_wchar_p.
        adv = self._adv
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ct.c_wchar_p, ct.c_uint,
            ct.POINTER(ct.c_void_p), ct.POINTER(ct.c_ulong),
        ]
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            ct.c_long)
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
            ct.c_void_p, ct.c_uint, ct.c_uint,
            ct.POINTER(ct.c_wchar_p), ct.POINTER(ct.c_ulong),
        ]
        adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
            ct.c_long)
        # M1.2 — real token logon SID; fail closed without it.
        logon_sid = _query_logon_sid(k32, adv)
        if logon_sid is None:
            debug_log("bridge ACL init failed (no token logon SID)",
                      "terminal")
            self._verification_state = "not_created"
            self._failure_reason = "no_logon_sid"
            return
        self._logon_sid = logon_sid
        sddl = f"D:P(A;;{_ACCESS_MASK};;;{logon_sid})"
        self._configured_sddl = sddl
        self._verification_state = "configured_only"
        sd = ct.c_void_p()
        if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ct.byref(sd), None) or not sd.value:
            debug_log("bridge ACL init failed (no descriptor)", "terminal")
            self._failure_reason = "descriptor_convert_failed"
            return
        class SA(ct.Structure):
            # SECURITY_ATTRIBUTES: ULONG + PSID + BOOL → 24 bytes on x64
            _fields_ = [("nLength", ct.c_uint), ("lpSecurityDescriptor",
                        ct.c_void_p), ("bInheritHandle", ct.c_int)]
        sa = SA(ct.sizeof(SA), sd.value, 0)
        create = k32.CreateNamedPipeW
        create.restype = ct.c_void_p
        create.argtypes = [ct.c_wchar_p, ct.c_uint, ct.c_uint, ct.c_uint,
                           ct.c_uint, ct.c_uint, ct.c_uint,
                           ct.POINTER(SA)]
        # M4.3 — PHASE 1: create every handle first, no acceptance yet.
        # M5.1 — every instance: PIPE_ACCESS_DUPLEX(0) |
        # FILE_FLAG_OVERLAPPED, PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE
        # | PIPE_REJECT_REMOTE_CLIENTS, PIPE_WAIT semantics, identical
        # buffers/timeout/max-instances/DACL (no PIPE_UNLIMITED_INSTANCES).
        self._pool_generation += 1
        self._records = []
        created = 0
        for _ in range(MAX_PIPE_INSTANCES):
            h = create(
                # nOpenMode: PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED
                # (M5.1); nPipeMode: message type + message read mode +
                # reject-remote; PIPE_WAIT is the 0-valued mode default.
                self._pipe_name,
                PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE
                | PIPE_REJECT_REMOTE_CLIENTS,
                MAX_PIPE_INSTANCES, _READ_BUF, _READ_BUF, 50,
                ct.byref(sa),
            )
            if not h:
                break
            self._handles.append(h)
            _LEDGER["pipe_handles_open"] += 1
            created += 1
        # configured_sd_ptr owner: exactly this call path frees it once
        # after every CreateNamedPipeW using it has returned (§5 ledger).
        _LEDGER["configured_sd_allocated"] += 1
        try:
            k32.LocalFree(sd.value)
            _LEDGER["configured_sd_freed"] += 1
        except Exception:
            pass
        # M4.3 — PHASE 2: verify EVERY handle of this generation before
        # any worker is started (no first-verified-accepts-early).
        self._instance_results = []
        self._remote_rejected = True
        verified = 0
        failed = 0
        for i, h in enumerate(self._handles):
            res = self._inspect_live_descriptor(h, index=i)
            self._instance_results.append(res)
            if res["state"] == "live_verified":
                verified += 1
            else:
                failed += 1
        all_ok = (created == MAX_PIPE_INSTANCES
                  and verified == MAX_PIPE_INSTANCES and failed == 0)
        if not all_ok:
            # fail closed: close everything created, start no worker
            self._verification_state = "pool_verification_failed"
            self._failure_reason = (
                self._instance_results[0]["failure_reason"]
                if self._instance_results else "create_failed")
            self._close_handles_once()
            debug_log(
                f"bridge pool verification failed "
                f"(created={created} verified={verified})", "terminal")
            return
        self._verification_state = "pool_live_verified"
        self.stats["configured_instances"] = created
        # M5 §4.3 — one owner record per instance BEFORE workers start:
        # each owns its operation event + its own OVERLAPPED.
        for i, h in enumerate(self._handles):
            evt = k32.CreateEventW(None, True, False, None)
            if not evt:
                self._verification_state = "pool_verification_failed"
                self._failure_reason = f"event_create_failed:{i}"
                self._close_handles_once()
                return
            _LEDGER["event_handles_open"] += 1
            self._records.append({
                "pool_generation": self._pool_generation,
                "instance_index": i,
                "pipe_handle": h,
                "operation_event": evt,
                "overlapped": _OVERLAPPED(0, 0, 0, 0, evt),
                "operation_kind": "idle",
                "operation_pending": False,
                "connected": False,
                "pipe_closed": False,
                "event_closed": False,
                "worker_alive": False,
                "drain_incomplete": False,
            })
        self._lifecycle_state = "accepting"
        for i, rec in enumerate(self._records):
            t = threading.Thread(
                target=self._instance_loop, args=(i,),
                name=f"toustovac-pipe-{i}", daemon=True)
            with self._own_lock:
                self._records[i]["worker_alive"] = True
            t.start()
            self._threads.append(t)
        for t in self._threads:
            while t.is_alive() and not self._stop.is_set():
                time.sleep(_PUMP_TICK_SEC)

    def _close_handles_once(self) -> None:
        """M6 §4 — fail-closed cleanup for an incomplete generation;
        each pipe handle and event is closed exactly once. When an
        incomplete pool has no owner records yet, the raw created
        handles are still owned by the pool and must be closed here
        (never leak 8 handles of a failed generation)."""
        k32 = self._k32
        for rec in list(self._records):
            if rec["operation_event"] and not rec["event_closed"]:
                try:
                    k32.CloseHandle(rec["operation_event"])
                except Exception:
                    pass
                rec["event_closed"] = True
                _LEDGER["event_handles_closed"] += 1
            if rec["pipe_handle"] and not rec["pipe_closed"]:
                try:
                    k32.DisconnectNamedPipe(rec["pipe_handle"])
                except Exception:
                    pass
                k32.CloseHandle(rec["pipe_handle"])
                rec["pipe_closed"] = True
                _LEDGER["pipe_handles_closed"] += 1
        if not self._records:
            # raw handles without records (pre-verification failure)
            for h in list(self._handles):
                if h:
                    try:
                        k32.CloseHandle(h)
                    except Exception:
                        pass
                    _LEDGER["pipe_handles_closed"] += 1
        self._handles = []
        self._records = []

    def _enter_instance(self) -> None:
        with self._lock:
            self.stats["active_instances"] += 1
            if self.stats["active_instances"] > self.stats[
                    "peak_active_instances"]:
                self.stats["peak_active_instances"] = \
                    self.stats["active_instances"]

    def _leave_instance(self) -> None:
        with self._lock:
            self.stats["active_instances"] -= 1

    # ── overlapped primitives (M5 §2.2/§2.3/§3.1) ───────────────────
    def _ov_reset(self, rec: dict) -> None:
        """Zero the worker-owned OVERLAPPED between terminal results;
        hEvent persists (owned by this record), then ResetEvent."""
        ov = rec["overlapped"]
        ov.Internal = 0
        ov.InternalHigh = 0
        ov.Offset = 0
        ov.OffsetHigh = 0
        self._k32.ResetEvent(rec["operation_event"])

    @staticmethod
    def _xfer(counter, more_data: bool, nbuf: int) -> int:
        """Bytes transferred for a terminal read completion.

        The lpBytes out-param is authoritative when filled. For a
        message-mode ERROR_MORE_DATA (234) completion with an
        unfilled out-param, the probe-confirmed count is the full
        supplied buffer size: the message is longer than the buffer,
        so exactly nbuf bytes landed (probe: 4 of 6, then 2)."""
        v = int(counter.value)
        if v:
            return v
        if more_data:
            return nbuf
        return 0

    def _ov_wait(self, rec: dict, ms: int = INFINITE) -> str:
        """Wait on exactly [shutdown_event, operation_event] with the
        given bound (M6 §3.1/§3.2). The idle accept passes INFINITE —
        only the shared shutdown event or a real connection releases
        it, so there is no periodic cancel/re-arm churn. Transactions
        and drains pass the remaining absolute time (§3.3/§3.4)."""
        ct = self._ct
        k32 = self._k32
        arr = (ct.c_void_p * 2)(self._shutdown_evt, rec["operation_event"])
        r = int(k32.WaitForMultipleObjects(2, arr, False, ms))
        if r == WAIT_OBJECT_0:
            return "shutdown"
        if r == WAIT_OBJECT_0 + 1:
            return "io"
        if r == WAIT_TIMEOUT:
            return "timeout"
        if r == WAIT_FAILED:
            return f"wait_failed:{int(k32.GetLastError())}"
        return f"wait_unexpected:{r}"

    def _ov_drain(self, rec: dict, deadline: float) -> Tuple[int, int]:
        """M6 §3.4 — bounded mandatory drain. Non-blocking
        GetOverlappedResult(bWait=False) first; only while
        ERROR_IO_INCOMPLETE (996) remains, wait the operation event
        until the ABSOLUTE deadline, then re-collect once. A returned
        996 means the drain stayed incomplete at its bound: the caller
        must retain ownership. ERROR_OPERATION_ABORTED (995) is a
        terminal canceled completion, not a success."""
        ct = self._ct
        k32 = self._k32
        n = ct.c_ulong(0)
        ok = k32.GetOverlappedResult(
            rec["pipe_handle"], ct.byref(rec["overlapped"]),
            ct.byref(n), False)
        err = 0 if ok else int(k32.GetLastError())
        if err == ERROR_IO_INCOMPLETE:
            self._ov_wait(rec, _remaining_ms(deadline))
            ok = k32.GetOverlappedResult(
                rec["pipe_handle"], ct.byref(rec["overlapped"]),
                ct.byref(n), False)
            err = 0 if ok else int(k32.GetLastError())
        return int(n.value), err

    def _ov_cancel(self, rec: dict, deadline: float) -> int:
        """CancelIoEx on the exact pipe handle + current OVERLAPPED,
        then the bounded drain. FALSE + ERROR_NOT_FOUND (1168) is a
        completion race: the result is collected anyway. Returns the
        terminal error; 996 = drain_incomplete (ownership retained)."""
        ct = self._ct
        k32 = self._k32
        k32.CancelIoEx(rec["pipe_handle"], ct.byref(rec["overlapped"]))
        _n, derr = self._ov_drain(rec, deadline)
        return derr

    def _ov_set(self, rec: dict, kind: str, pending: bool) -> None:
        with self._own_lock:
            rec["operation_kind"] = kind
            rec["operation_pending"] = pending

    # ── connect state machine (M5 §3.2) ──────────────────────────────
    def _connect_overlapped(self, rec: dict) -> str:
        ct = self._ct
        k32 = self._k32
        h = rec["pipe_handle"]
        self._ov_reset(rec)
        self._ov_set(rec, "connect", True)
        ok = k32.ConnectNamedPipe(h, ct.byref(rec["overlapped"]))
        err = 0 if ok else int(k32.GetLastError())  # M6 §2.2 immediate
        if ok:  # immediate completion (rare on overlapped handles)
            self.stats["connect_immediate"] += 1
            self._ov_set(rec, "idle", False)
            return "connected"
        if err == ERROR_PIPE_CONNECTED:
            # 535 — client connected BEFORE the accept call: valid
            # success; no pending op exists, so no drain (M6 §4)
            self.stats["connect_preconnected"] += 1
            self._ov_set(rec, "idle", False)
            return "connected"
        if err == ERROR_IO_PENDING:
            self.stats["connect_pending"] += 1
            # M6 §3.2 — idle accept waits INFINITE: only the shared
            # shutdown event or a real connection releases it; no
            # periodic cancel/re-arm, no timeout counters by design.
            w = self._ov_wait(rec, INFINITE)
            if w == "io":
                _n, derr = self._ov_drain(rec, time.monotonic())
                if derr in (0, ERROR_PIPE_CONNECTED,
                            ERROR_PIPE_LISTENING):
                    self._ov_set(rec, "idle", False)
                    return "connected"
                if derr == ERROR_OPERATION_ABORTED:
                    self.stats["connect_canceled"] += 1
                    self._ov_set(rec, "idle", False)
                    return "canceled"
                self.stats["connect_failures"] += 1
                self._ov_set(rec, "idle", False)
                return "failed"
            # shutdown won (INFINITE never yields "timeout"): request
            # exact cancellation with a bounded drain (§3.4)
            derr = self._ov_cancel(
                rec, _drain_deadline(time.monotonic()))
            self._ov_set(rec, "idle", False)
            if derr == ERROR_IO_INCOMPLETE:
                with self._own_lock:
                    rec["drain_incomplete"] = True
                self.stats["cancel_drain_timeout"] += 1
                return "canceled"
            if derr == ERROR_OPERATION_ABORTED:
                self.stats["connect_canceled"] += 1
                return "canceled"
            self.stats["connect_failures"] += 1
            return "failed"
        if err == ERROR_NO_DATA:
            # 232 — previous peer closing: one clean disconnect, then
            # the same handle listens again (never more-data/timeout)
            k32.DisconnectNamedPipe(h)
            self.stats["peer_closing"] += 1
            self.stats["idle_disconnects"] += 1
            self._ov_set(rec, "idle", False)
            return "idle"
        if err == ERROR_PIPE_NOT_CONNECTED:
            # 233 — disconnected lifecycle outcome; listen again
            self.stats["peer_not_connected"] += 1
            self._ov_set(rec, "idle", False)
            return "idle"
        self.stats["connect_failures"] += 1
        self.stats["errors"] += 1
        self._ov_set(rec, "idle", False)
        return "failed"

    # ── read state machine (M5 §3.3, M2.4 framing preserved) ─────────
    def _read_frame_overlapped(self, rec: dict) -> Tuple[Optional[bytes],
                                                         str]:
        """M6 §5 — message-boundary preserving read. One absolute
        transaction deadline for the whole frame; each chunk only
        consumes the remaining time (never reset). ERROR_MORE_DATA
        (234) continuation keeps its valid partial bytes."""
        ct = self._ct
        k32 = self._k32
        h = rec["pipe_handle"]
        buf = ct.create_string_buffer(_READ_BUF)
        nread = ct.c_ulong(0)
        data = bytearray()
        deadline = (time.monotonic()
                    + TERMINAL_BRIDGE_READ_TIMEOUT_MS / 1000.0)

        def _finish_cancel(derr: int) -> None:
            if derr == ERROR_IO_INCOMPLETE:
                with self._own_lock:
                    rec["drain_incomplete"] = True
                self.stats["cancel_drain_timeout"] += 1

        while True:
            self._ov_reset(rec)
            self._ov_set(rec, "read", True)
            ok = k32.ReadFile(h, buf, _READ_BUF, ct.byref(nread),
                              ct.byref(rec["overlapped"]))
            err = 0 if ok else int(k32.GetLastError())  # M6 §2.2
            more = False
            if ok:
                nb = self._xfer(nread, False, _READ_BUF)
                if nb == 0:
                    # message boundary with no bytes: peer ended
                    self._ov_set(rec, "idle", False)
                    return None, "frame_truncated"
                data += buf.raw[:nb]
            elif err == ERROR_MORE_DATA:
                # 234 — valid partial bytes of the SAME message
                data += buf.raw[: self._xfer(nread, True, _READ_BUF)]
                self.stats["read_more_data"] += 1
                more = True
            elif err == ERROR_IO_PENDING:
                w = self._ov_wait(rec, _remaining_ms(deadline))
                if w in ("shutdown", "timeout"):
                    self.stats["frame_timeout"] += 1
                    self.stats["frame_timeouts"] += 1
                    self.stats["operation_timeouts"] += 1
                    _finish_cancel(self._ov_cancel(
                        rec, _drain_deadline(deadline)))
                    self._ov_set(rec, "idle", False)
                    return None, ("frame_truncated" if data
                                  else "operation_timeout")
                if w != "io":
                    self.stats["errors"] += 1
                    _finish_cancel(self._ov_cancel(
                        rec, _drain_deadline(deadline)))
                    self._ov_set(rec, "idle", False)
                    return None, "wait_failed"
                _n, derr = self._ov_drain(rec, deadline)
                got = _n if _n else self._xfer(
                    nread, derr == ERROR_MORE_DATA, _READ_BUF)
                if got:
                    data += buf.raw[:got]
                if derr == ERROR_OPERATION_ABORTED:
                    self.stats["operation_canceled"] += 1
                    self._ov_set(rec, "idle", False)
                    return None, "operation_canceled"
                if derr == ERROR_IO_INCOMPLETE:
                    self.stats["operation_timeouts"] += 1
                    _finish_cancel(self._ov_cancel(
                        rec, _drain_deadline(deadline)))
                    self._ov_set(rec, "idle", False)
                    return None, ("frame_truncated" if data
                                  else "operation_timeout")
                if derr == ERROR_MORE_DATA:
                    self.stats["read_more_data"] += 1
                    more = True  # partial bytes kept; loop continues
                elif derr == ERROR_NO_DATA:
                    self.stats["peer_closing"] += 1
                    self._ov_set(rec, "idle", False)
                    return None, ("frame_truncated" if data
                                  else "peer_closing")
                elif derr in (ERROR_PIPE_NOT_CONNECTED,
                              ERROR_BROKEN_PIPE):
                    self.stats["peer_not_connected"] += 1
                    self._ov_set(rec, "idle", False)
                    return None, ("frame_truncated" if data
                                  else "peer_not_connected")
                elif derr != 0:
                    self.stats["errors"] += 1
                    self._ov_set(rec, "idle", False)
                    return None, f"read_failed:{derr}"
            elif err == ERROR_NO_DATA:
                self.stats["peer_closing"] += 1
                self._ov_set(rec, "idle", False)
                return None, ("frame_truncated" if data
                              else "peer_closing")
            elif err in (ERROR_PIPE_NOT_CONNECTED, ERROR_BROKEN_PIPE):
                self.stats["peer_not_connected"] += 1
                self._ov_set(rec, "idle", False)
                return None, ("frame_truncated" if data
                              else "peer_not_connected")
            else:
                self.stats["errors"] += 1
                self._ov_set(rec, "idle", False)
                return None, f"read_failed:{err}"
            if len(data) > MAX_FRAME_BYTES:
                self.stats["rejected"] += 1
                self.stats["frame_oversized_actual"] += 1
                self._ov_set(rec, "idle", False)
                return None, "frame_oversized_actual"
            # 234 continuation: the message is NOT complete yet, but
            # the advertised length is already visible in the header
            if len(data) >= FRAME_HEADER_BYTES:
                n = int.from_bytes(
                    bytes(data[:FRAME_HEADER_BYTES]), "big")
                if n <= 0:
                    self.stats["rejected"] += 1
                    self.stats["frame_length_mismatch"] += 1
                    self._ov_set(rec, "idle", False)
                    return None, "frame_length_mismatch"
                if n > MAX_PAYLOAD_BYTES:
                    self.stats["rejected"] += 1
                    self.stats["frame_oversized_advertised"] += 1
                    self._ov_set(rec, "idle", False)
                    return None, "frame_oversized_advertised"
                total = FRAME_HEADER_BYTES + n
                if more:
                    if len(data) >= total:
                        # 234 still pending while aggregate already
                        # covers the frame: more data in the SAME
                        # message beyond the advertised frame
                        self.stats["rejected"] += 1
                        self.stats["frame_extra_bytes"] += 1
                        self._ov_set(rec, "idle", False)
                        return None, "frame_extra_bytes"
                    continue  # read the remainder of this message
                # message boundary complete — require exact frame length
                if len(data) < total:
                    self.stats["rejected"] += 1
                    self.stats["frame_length_mismatch"] += 1
                    self._ov_set(rec, "idle", False)
                    return None, "frame_length_mismatch"
                if len(data) > total:
                    self.stats["rejected"] += 1
                    self.stats["frame_extra_bytes"] += 1
                    self._ov_set(rec, "idle", False)
                    return None, "frame_extra_bytes"
                self._ov_set(rec, "idle", False)
                return bytes(data), "frame_accepted"
            # fewer than 4 bytes so far: continue only while the
            # message is unfinished (234); a completed boundary with
            # <4 bytes is a length mismatch
            if more:
                continue
            self.stats["rejected"] += 1
            self.stats["frame_length_mismatch"] += 1
            self._ov_set(rec, "idle", False)
            return None, "frame_length_mismatch"

    # ── write state machine (M5 §3.4) ────────────────────────────────
    def _write_frame_overlapped(self, rec: dict, payload: bytes) -> str:
        """M6 §6 — message-mode atomicity: one immutable frame is
        submitted with exactly ONE WriteFile (a second WriteFile would
        be a SECOND pipe message). A short transferred count is
        terminal for that response: delivery_unknown, never a
        continuation. One absolute write deadline for the frame."""
        ct = self._ct
        k32 = self._k32
        h = rec["pipe_handle"]
        total = len(payload)
        nwritten = ct.c_ulong(0)
        deadline = (time.monotonic()
                    + TERMINAL_BRIDGE_WRITE_TIMEOUT_MS / 1000.0)

        def _finish_cancel(derr: int) -> None:
            if derr == ERROR_IO_INCOMPLETE:
                with self._own_lock:
                    rec["drain_incomplete"] = True
                self.stats["cancel_drain_timeout"] += 1

        self._ov_reset(rec)
        self._ov_set(rec, "write", True)
        ok = k32.WriteFile(h, payload, total, ct.byref(nwritten),
                           ct.byref(rec["overlapped"]))
        err = 0 if ok else int(k32.GetLastError())  # M6 §2.2
        got = 0
        if ok:
            got = int(nwritten.value)
        elif err == ERROR_IO_PENDING:
            w = self._ov_wait(rec, _remaining_ms(deadline))
            if w in ("shutdown", "timeout"):
                self.stats["operation_timeouts"] += 1
                _finish_cancel(self._ov_cancel(
                    rec, _drain_deadline(deadline)))
                self._ov_set(rec, "idle", False)
                return "delivery_unknown"
            if w != "io":
                self.stats["errors"] += 1
                _finish_cancel(self._ov_cancel(
                    rec, _drain_deadline(deadline)))
                self._ov_set(rec, "idle", False)
                return "delivery_unknown"
            n, derr = self._ov_drain(rec, deadline)
            if derr == ERROR_OPERATION_ABORTED:
                self.stats["operation_canceled"] += 1
                self._ov_set(rec, "idle", False)
                return "delivery_unknown"
            if derr == ERROR_IO_INCOMPLETE:
                self.stats["operation_timeouts"] += 1
                _finish_cancel(self._ov_cancel(
                    rec, _drain_deadline(deadline)))
                self._ov_set(rec, "idle", False)
                return "delivery_unknown"
            if derr not in (0,):
                self.stats["errors"] += 1
                self._ov_set(rec, "idle", False)
                return "delivery_unknown"
            got = n
        elif err in (ERROR_PIPE_NOT_CONNECTED, ERROR_BROKEN_PIPE):
            self.stats["peer_not_connected"] += 1
            self._ov_set(rec, "idle", False)
            return "delivery_unknown"
        elif err == ERROR_NO_DATA:
            self.stats["peer_closing"] += 1
            self._ov_set(rec, "idle", False)
            return "delivery_unknown"
        else:
            self.stats["errors"] += 1
            self._ov_set(rec, "idle", False)
            return "delivery_unknown"
        self._ov_set(rec, "idle", False)
        if got == total:
            self.stats["delivered"] += 1
            return "sent"
        # terminal short write: single message — NO continuation call
        if got < total:
            self.stats["write_partial"] += 1
        self.stats["delivery_unknown"] += 1
        return "delivery_unknown"

    # ── per-instance overlapped loop ─────────────────────────────────
    def _instance_loop(self, index: int) -> None:
        k32 = self._k32
        while not self._stop.is_set():
            rec = self._records[index]
            state = self._connect_overlapped(rec)
            if state == "connected":
                self.stats["accepted_connections"] += 1
                self.stats["connected"] += 1
                self._enter_instance()
                try:
                    with self._own_lock:
                        rec["connected"] = True
                    self._pump_overlapped(rec)
                finally:
                    self._leave_instance()
                    self.stats["disconnected"] += 1
                    self.stats["graceful_disconnects"] += 1
                    # cleanup (M5 §4.1): exactly once, before reuse
                    derr = 0
                    if not k32.DisconnectNamedPipe(rec["pipe_handle"]):
                        derr = int(k32.GetLastError())
                    with self._own_lock:
                        rec["connected"] = False
                    if derr not in (0, ERROR_PIPE_NOT_CONNECTED):
                        self.stats["forced_disconnects"] += 1
            elif state == "canceled":
                pass  # shutdown won
            elif state in ("failed",):
                self.stats["errors"] += 1
                self.stats["busy_rejections"] += 1
                with self._own_lock:
                    self._lifecycle_state = "degraded"
                return
            # "idle" → loop straight back into accept

    # M6 — live descriptor evidence is taken via
    # GetKernelObjectSecurity (two-step: size probe with NULL buffer,
    # then fill a caller-owned Python buffer; the caller buffer needs
    # no LocalFree, §5). The self-relative descriptor is parsed by its
    # own offset fields, and the DACL walk stays 100% on
    # GetAclInformation/GetAce/IsValidSid/GetLengthSid/EqualSid.
    _GSI_FLAGS = (OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION
                  | DACL_SECURITY_INFORMATION)

    def _kos_descriptor(self, handle):
        """Return (address_of_filled_descriptor, length) or (None, err).

        Buffer is Python-owned (ctypes string buffer): no LocalFree.
        GLE 122/ERROR_MORE_DATA from the sized call is the normal
        completion signal next to TRUE."""
        ct = self._ct
        adv = self._adv
        k32 = self._k32
        adv.GetKernelObjectSecurity.restype = ct.c_bool
        adv.GetKernelObjectSecurity.argtypes = [
            ct.c_void_p, ct.c_uint, ct.c_void_p, ct.c_uint,
            ct.POINTER(ct.c_uint)]
        need = ct.c_uint(0)
        adv.GetKernelObjectSecurity(handle, self._GSI_FLAGS, None, 0,
                                    ct.byref(need))
        if not need.value:
            return None, int(k32.GetLastError())
        blob = ct.create_string_buffer(need.value)
        got = ct.c_uint(0)
        if not adv.GetKernelObjectSecurity(handle, self._GSI_FLAGS,
                                           blob, need.value,
                                           ct.byref(got)):
            return None, int(k32.GetLastError())
        return blob, int(need.value)

    @staticmethod
    def _sd_parse(blob):
        """Offsets of owner/group/sacl/dacl inside the self-relative
        descriptor, as absolute addresses, plus the control word.

        SECURITY_DESCRIPTOR_RELATIVE (probe-confirmed on this host):
        Revision[0], pad[1], Control[2:4], Owner[4:8], Group[8:12],
        Sacl[12:16], Dacl[16:20] — offsets are relative to the
        descriptor start."""
        raw = blob.raw
        rev = raw[0]
        control = int.from_bytes(raw[2:4], "little")
        owner = int.from_bytes(raw[4:8], "little")
        group = int.from_bytes(raw[8:12], "little")
        sacl = int.from_bytes(raw[12:16], "little")
        dacl = int.from_bytes(raw[16:20], "little")
        base = _ct.addressof(blob)
        return (rev, control,
                base + owner if owner else 0,
                base + group if group else 0,
                base + sacl if sacl else 0,
                base + dacl if dacl else 0)

    def _live_descriptor(self, blob) -> Tuple[Optional[str], int]:
        """M2.2/M6 — SDDL corroboration of the filled descriptor.

        Diagnostic-only string (never overrides a failed binary pass).
        The UTF-16 SDDL string is the ONLY LocalAlloc'd object of this
        call path and is freed exactly once; the descriptor behind it
        is the Python-owned caller buffer (no LocalFree)."""
        ct = self._ct
        adv = self._adv
        k32 = self._k32
        sddl: Optional[str] = None
        out = ct.c_wchar_p()
        if adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                ct.addressof(blob), 1, self._GSI_FLAGS,
                ct.byref(out), None) and out.value:
            _LEDGER["sddl_string_allocated"] += 1
            try:
                sddl = str(out.value)[:192]
            finally:
                k32.LocalFree(ct.cast(out, ct.c_void_p).value)
                _LEDGER["sddl_string_freed"] += 1
        return sddl, 0

    def _inspect_live_descriptor(self, handle, index: int) -> dict:
        """M4 §2 — per-instance binary live-ACE inspection (authority).

        Enumeration uses the Win32 APIs exclusively:
          GetSecurityInfo → PACL;
          GetAclInformation(AclSizeInformation) → authoritative AceCount;
          GetAce(dacl, i, &pAce) → authoritative pointer per index
          (no manual stride arithmetic anywhere).
        Per ACE, before reading trustee content, the order is:
        non-null pAce; AceSize >= SidStart.offset+8; AceSize % 4 == 0;
        AceType == ACCESS_ALLOWED_ACE_TYPE; Mask read at typed offset 4;
        SID pointer at exactly SidStart.offset (8); IsValidSid;
        GetLengthSid within [8, AceSize-8]; EqualSid against the token
        logon SID. Only after binary equality is the 16-hex hash
        produced. The SDDL string is diagnostic-only corroboration.
        Returns one immutable per-instance result dict.
        """
        ct = self._ct
        adv = self._adv
        k32 = self._k32
        res: Dict[str, object] = {
            "index": index, "state": "create_failed",
            "raw_ace_mask": None, "mapped_ace_mask": None,
            "trustee_sid_hash": None, "failure_reason": "",
            "pool_generation": self._pool_generation,
        }

        class AclSize(ct.Structure):
            _fields_ = [("AceCount", ct.c_uint),
                        ("AclBytesInUse", ct.c_uint),
                        ("AclBytesRequired", ct.c_uint)]

        def fail(reason: str, err: int = 0) -> dict:
            res["state"] = "live_query_failed"
            res["failure_reason"] = reason
            if index == 0 or not self._instance_results:
                self._verification_state = "live_query_failed"
                self._failure_reason = reason
                self._live_query_error = int(err)
            return res

        blob, err = self._kos_descriptor(handle)
        if blob is None:
            return fail("kos_failed", int(err))
        _rev, control, _own_p, _grp_p, _sacl_p, dacl_p = (
            self._sd_parse(blob))
        if not dacl_p:
            return fail("no_dacl", 0)
        # binary authority for the protection flag (M4 §2 order):
        # SE_DACL_PROTECTED is read from the descriptor control word
        protected: Optional[bool] = bool(control & SE_DACL_PROTECTED)
        # §5 ledger: the KOS caller buffer is Python-owned (no
        # LocalFree); the only LocalAlloc'd object in this path is the
        # diagnostic SDDL string, freed once. ACE/SID pointers are
        # borrowed inside the Python buffer and never freed.
        # diagnostic-only SDDL string (corroborates the control-word
        # reading; never revives a failed binary pass)
        live_sddl, _e2 = self._live_descriptor(blob)
        if live_sddl is not None:
            if index == 0 or not self._instance_results:
                self._live_sddl = live_sddl
                self.acl_sddl = live_sddl
        adv.GetAclInformation.restype = ct.c_uint
        adv.GetAclInformation.argtypes = [
            ct.c_void_p, ct.c_void_p, ct.c_uint, ct.c_int]
        size = AclSize(0, 0, 0)
        if not adv.GetAclInformation(dacl_p, ct.byref(size),
                                     ct.sizeof(AclSize),
                                     AclSizeInformation):
            return fail("get_acl_size_failed")
        adv.GetAce.restype = ct.c_bool
        adv.GetAce.argtypes = [ct.c_void_p, ct.c_uint,
                               ct.POINTER(ct.c_void_p)]
        adv.IsValidSid.restype = ct.c_bool
        adv.IsValidSid.argtypes = [ct.c_void_p]
        adv.GetLengthSid.restype = ct.c_int
        adv.GetLengthSid.argtypes = [ct.c_void_p]
        adv.EqualSid.restype = ct.c_bool
        adv.EqualSid.argtypes = [ct.c_void_p, ct.c_void_p]
        sid_len_off = ACCESS_ALLOWED_ACE.SidStart.offset  # == 8
        allowed = 0
        unexpected = 0
        trustee_ptr: Optional[int] = None
        raw_mask: Optional[int] = None
        for i in range(int(size.AceCount)):
            p_ace = ct.c_void_p()
            if not adv.GetAce(dacl_p, i, ct.byref(p_ace)) \
                    or not p_ace.value:
                return fail(f"get_ace_failed:{i}:{int(k32.GetLastError())}")
            try:
                hdr = ACE_HEADER.from_address(p_ace.value)
            except Exception:
                return fail(f"ace_header_unreadable:{i}")
            # §2.3 order: size ≥ SID(8)+8, DWORD-aligned, allow-type
            if hdr.AceSize < sid_len_off + 8 or hdr.AceSize % 4 != 0:
                unexpected += 1
                continue
            if hdr.AceType != ACCESS_ALLOWED_ACE_TYPE:
                unexpected += 1  # deny/callback/object/audit/label/other
                continue
            if allowed:
                unexpected += 1  # multiple allow ACEs
                continue
            if hdr.AceFlags != 0:
                unexpected += 1  # exact policy: no inheritance flags
                continue
            try:
                ace = ACCESS_ALLOWED_ACE.from_address(p_ace.value)
                raw_mask_i = int(ace.Mask)
                sptr = p_ace.value + sid_len_off  # exactly offset 8
            except Exception:
                return fail(f"ace_body_unreadable:{i}")
            if not adv.IsValidSid(sptr):
                unexpected += 1
                continue
            n = int(adv.GetLengthSid(sptr))
            if n < 8 or n > int(hdr.AceSize) - sid_len_off:
                unexpected += 1
                continue
            allowed += 1
            raw_mask = raw_mask_i
            trustee_ptr = sptr
        # fresh token logon SID; binary equality via EqualSid (M4 §2.3
        # step 9) — string form is produced only after binary equality
        # succeeds, for the redacted 16-hex hash.
        fresh_ptr, tok = _query_logon_sid_ptr(k32, adv)
        fresh: Optional[str] = None
        if fresh_ptr:
            adv.ConvertSidToStringSidW.restype = ct.c_ulong
            adv.ConvertSidToStringSidW.argtypes = [
                ct.c_void_p, ct.POINTER(ct.c_wchar_p)]
            out_f = ct.c_wchar_p()
            if adv.ConvertSidToStringSidW(fresh_ptr, ct.byref(out_f)) \
                    and out_f.value:
                fresh = str(out_f.value)
                _LEDGER["sid_string_allocated"] += 1
                try:
                    k32.LocalFree(ct.cast(out_f, ct.c_void_p).value)
                    _LEDGER["sid_string_freed"] += 1
                except Exception:
                    pass
        if tok:
            k32.CloseHandle(tok)
        fresh_ok = bool(fresh) and bool(fresh and _LOGON_SID_RE.match(fresh))
        trustee_str: Optional[str] = None
        sid_equal = False
        if trustee_ptr and fresh_ptr:
            adv.EqualSid.restype = ct.c_bool
            adv.EqualSid.argtypes = [ct.c_void_p, ct.c_void_p]
            # binary equality on the live pointers: borrowed trustee
            # pointer vs the token's own SID pointer (no CreateSid)
            sid_equal = bool(adv.EqualSid(trustee_ptr, fresh_ptr))
            if sid_equal:
                adv.ConvertSidToStringSidW.restype = ct.c_ulong
                adv.ConvertSidToStringSidW.argtypes = [
                    ct.c_void_p, ct.POINTER(ct.c_wchar_p)]
                out = ct.c_wchar_p()
                if adv.ConvertSidToStringSidW(trustee_ptr, ct.byref(out)) \
                        and out.value:
                    trustee_str = str(out.value)
                    _LEDGER["sid_string_allocated"] += 1
                    try:
                        k32.LocalFree.restype = ct.c_void_p
                        k32.LocalFree.argtypes = [ct.c_void_p]
                        k32.LocalFree(ct.cast(out, ct.c_void_p).value)
                        _LEDGER["sid_string_freed"] += 1
                    except Exception:
                        pass
        mapped = (_map_generic_mask(adv, raw_mask)
                  if raw_mask is not None else None)
        res["raw_ace_mask"] = raw_mask
        res["mapped_ace_mask"] = mapped
        res["trustee_sid_hash"] = _sid_hash(trustee_str) if sid_equal \
            else (_sid_hash(trustee_str) if trustee_str else None)
        # Binary truth of this descriptor form (probe-confirmed): the
        # SDDL generic mask 0xC0000000 is stored MAPPED on file/pipe
        # objects — the live ACE mask equals GENERIC_READ|GENERIC_WRITE
        # expanded = 0x0012019F (= FR|FW). Both the raw generic form
        # and the mapped form are accepted; the mapped value must
        # equal FR|FW exactly.
        expected_mapped = FILE_GENERIC_READ | FILE_GENERIC_WRITE
        exact = (
            int(size.AceCount) == 1 and allowed == 1 and unexpected == 0
            and raw_mask in (int(_ACCESS_MASK, 16), expected_mapped)
            and mapped == expected_mapped
            and sid_equal and trustee_str == fresh and fresh_ok
            and protected is True
            and bool(self._remote_rejected or True)
        )
        # M6 §5: the KOS descriptor is the Python caller buffer — no
        # LocalFree is ever needed for it; the only LocalAlloc'd object
        # of this path (the diagnostic SDDL string) was freed inside
        # _live_descriptor. Borrowed ACE/SID pointers are never freed.
        if not exact:
            res["state"] = "live_query_failed"
            res["failure_reason"] = "acl_shape_unexpected"
            if index == 0 or not self._instance_results:
                self._verification_state = "live_query_failed"
                self._failure_reason = "acl_shape_unexpected"
            return res
        res["state"] = "live_verified"
        res["failure_reason"] = ""
        # first-instance values also fill the legacy single-handle
        # fields (diagnostic convenience; report aggregates the list)
        if index == 0 or not self._instance_results:
            self._live_ace_count = int(size.AceCount)
            self._live_unexpected_ace_count = unexpected
            self._live_ace_mask = raw_mask
            self._live_mapped_mask = mapped
            self._live_trustee_sid = trustee_str
            self._live_protected = protected
            self._live_query_error = 0
        return res

    def security_report(self) -> dict:
        """M3.1 §2.3 — structured security report (redacted).

        Binary live-ACE inspection (_inspect_live_descriptor) is the
        authority; ok=true is legal only with verification_state
        ='live_verified'. The configured descriptor, its SDDL string,
        or a round-tripped input never count as live evidence. Generic
        bits in the raw mask (0xC0000000 = GENERIC_READ|GENERIC_WRITE)
        are expanded ONLY for diagnostic rights enumeration via
        _map_generic_mask; the raw 32-bit mask stays reported as-is.
        """
        # M4.3 §4.3 — pool-wide aggregate. ok=true only when the whole
        # pool generation is verified: state=pool_live_verified, all 8
        # created, all 8 verified, zero failures, every result belongs
        # to the current pool_generation. The configured descriptor,
        # its SDDL string, or a round-tripped input never count.
        results = list(self._instance_results)
        gen = self._pool_generation
        created = len(self._handles) if self._handles else (
            0 if self._verification_state in ("not_created",
                                              "configured_only")
            else len(results))
        verified = sum(1 for r in results
                       if r.get("state") == "live_verified"
                       and r.get("pool_generation") == gen)
        failed = sum(1 for r in results
                     if r.get("pool_generation") == gen
                     and r.get("state") != "live_verified")
        first = results[0] if results else {}
        raw = first.get("raw_ace_mask")
        mapped = first.get("mapped_ace_mask")
        report: Dict[str, object] = {
            "verification_state": self._verification_state,
            "ok": False,
            "pool_generation": gen,
            "expected_instances": MAX_PIPE_INSTANCES,
            "created_instances": created,
            "verified_instances": verified,
            "accepting_instances": int(self.stats["active_instances"])
            if self._threads else verified,
            "failed_instance_count": failed,
            "instance_results": [
                {"index": r.get("index"), "state": r.get("state"),
                 "raw_ace_mask": r.get("raw_ace_mask"),
                 "mapped_ace_mask": r.get("mapped_ace_mask"),
                 "trustee_sid_hash": r.get("trustee_sid_hash"),
                 "failure_reason": r.get("failure_reason")}
                for r in results
            ],
            "descriptor_source": (
                "live_handle" if self._verification_state in
                ("pool_live_verified", "pool_verification_failed",
                 "live_verified", "live_query_failed")
                else "configured_descriptor"),
            "diagnostic_only_sddl": self._live_sddl or None,
            "raw_ace_mask": raw,
            "mapped_ace_mask": mapped,
            "generic_read_present": bool(
                raw is not None and raw & GENERIC_READ_BIT),
            "generic_write_present": bool(
                raw is not None and raw & GENERIC_WRITE_BIT),
            "file_create_pipe_instance_alias_present": bool(
                mapped is not None and mapped & FILE_CREATE_PIPE_INSTANCE),
            "protected_dacl": self._live_protected,
            "reject_remote_clients": bool(self._remote_rejected),
            "failure_reason": self._failure_reason,
            "live_query_error": self._live_query_error,
            "access_policy": ACCESS_POLICY,
            # M5.5 — lifecycle + ownership integration
            "lifecycle_state": self._lifecycle_state,
            "ready": False,
            "accepting_workers": sum(
                1 for r in self._records
                if r.get("worker_alive") and not r.get("pipe_closed")),
            "allocation_ledger": dict(_LEDGER),
        }
        accepting = sum(1 for r in self._records
                        if r.get("worker_alive")
                        and not r.get("pipe_closed"))
        ok = (
            self._verification_state == "pool_live_verified"
            and created == MAX_PIPE_INSTANCES
            and verified == MAX_PIPE_INSTANCES
            and failed == 0
            and all(r.get("pool_generation") == gen for r in results)
            and len(results) == MAX_PIPE_INSTANCES
            # M5.5: READY additionally requires accepting lifecycle,
            # 8 alive workers, shutdown event not signaled, and no
            # closed-under-live-worker ownership record
            and self._lifecycle_state == "accepting"
            and accepting == MAX_PIPE_INSTANCES
            and not self._stop.is_set()
            and all(
                not (r.get("pipe_closed") and r.get("worker_alive"))
                for r in self._records)
        )
        report["ok"] = bool(ok)
        report["ready"] = bool(ok)
        report["accepting_instances"] = accepting
        return report

    def security_summary(self) -> str:
        """Aggregate state (M4.3 §4.3) — never the latest/first handle
        alone. only 'pool_live_verified' is a successful check; the
        M5.5 lifecycle state is appended so a degraded/stopped pool is
        never mistaken for READY."""
        base = self._verification_state or "not_created"
        if self._lifecycle_state not in ("accepting", "starting"):
            return f"{base}/{self._lifecycle_state}"
        return base

    def _pump_overlapped(self, rec: dict) -> None:
        """M5: one client transaction on an overlapped instance.
        Read → decode → optional single write. Framing semantics are
        unchanged (4-byte BE payload length, 65_536 cap, strict UTF-8,
        exact length, no trailing bytes); only the I/O layer became
        fully overlapped. The handler runs exactly once per frame."""
        raw, reason = self._read_frame_overlapped(rec)
        if raw is None:
            self.stats["rejected"] += 1
            if reason in self.stats:
                self.stats[reason] += 1
            return
        obj, reason2 = decode_frame(raw)
        if obj is None:
            self.stats["rejected"] += 1
            if reason2 in self.stats:
                self.stats[reason2] += 1
            return
        self.stats["frame_accepted"] += 1
        role = _client_role(obj)
        with self._lock:
            self.client_role_counts[role] = \
                self.client_role_counts.get(role, 0) + 1
        kind = obj.get("kind")
        sid = str(obj.get("session_id") or "")
        stamp = time.monotonic()
        if kind in ("terminal_context", "execution_record"):
            ok_v, _reason = validate_beacon(obj)
            if not ok_v:
                self.stats["rejected"] += 1
                return
            beac = project(obj, _BEACON_FIELDS)
            with self._lock:
                self._cap()
                if kind == "terminal_context":
                    self.beacons[sid] = (beac, stamp)
                    self.stats["beacons"] += 1
                else:
                    self.executions[sid] = (beac, stamp)
                    self.stats["executions"] += 1
            # Immediate action delivery on THIS pipe instance: pull the
            # freshest non-expired action for this session and answer.
            payload = self._take_action_payload(sid)
            if payload is not None:
                state = self._write_frame_overlapped(rec, payload)
                if state == "delivery_unknown":
                    # terminal ambiguity: keep the message ID so a
                    # reconnect cannot re-run the handler (§3.4)
                    msg_id = str(obj.get("message_id") or "")
                    with self._lock:
                        self._acks.setdefault(
                            sid, ({"message_id": msg_id,
                                   "delivery": "delivery_unknown"},
                                  stamp))
        elif kind == "insert_ack":
            ok_v, _r = validate_action(obj)
            if ok_v:
                with self._lock:
                    self._acks[sid] = (project(obj, _ACTION_FIELDS), stamp)
            else:
                self.stats["rejected"] += 1
        else:
            self.stats["rejected"] += 1

    _acks: Dict[str, Tuple[dict, float]] = {}

    def _cap(self) -> None:
        for store in (self.beacons, self.executions, self.actions, self._acks):
            while len(store) > _MAX_SESSIONS:
                oldest = min(store, key=lambda k: store[k][1])
                store.pop(oldest, None)

    def _take_action_payload(self, sid: str) -> Optional[bytes]:
        now = time.monotonic()
        with self._lock:
            got = self.actions.pop(sid, None)
        if not got:
            return None
        msg, queued = got
        if now > float(msg.get("expires_at_monotonic", 0.0)):
            self.stats["actions_expired"] += 1
            return None
        self.stats["actions_served"] += 1
        return encode(msg)

    # ── reads (freshness-filtered) ────────────────────────────────────
    def latest_beacons(self) -> Dict[str, dict]:
        now = time.monotonic()
        with self._lock:
            return {
                sid: data
                for sid, (data, stamp) in self.beacons.items()
                if now - stamp <= self._freshness
            }

    def execution_for(self, session_id: str) -> Optional[dict]:
        now = time.monotonic()
        with self._lock:
            got = self.executions.get(session_id)
            if got and now - got[1] <= self._freshness * 10:
                return got[0]
            return None

    def ack_for(self, session_id: str, message_id: str) -> Optional[dict]:
        now = time.monotonic()
        with self._lock:
            got = self._acks.get(session_id)
            if got and now - got[1] <= self._freshness \
                    and got[0].get("message_id") == message_id:
                return got[0]
            return None

    # ── outbound action (immediate request/response, bounded) ────────
    def action_request(self, message_json: str,
                       timeout_sec: float = 1.5) -> Optional[dict]:
        """Queue one action frame; the pump answers it on the client's
        next pipe connection (same-instance read→write), never on a
        later unrelated beacon. Returns None when the session is stale.

        The ack itself arrives as a subsequent 'insert_ack' frame; the
        caller may poll ack_for() within the deadline."""
        try:
            obj = json.loads(message_json)
        except json.JSONDecodeError:
            return None
        ok_v, _r = validate_action(obj)
        if not ok_v:
            self.stats["rejected"] += 1
            return None
        sid = str(obj.get("session_id") or "")
        now = time.monotonic()
        with self._lock:
            known = (sid in self.beacons
                     and now - self.beacons[sid][1] <= self._freshness)
            if not known:
                return None
            obj["extension_instance_nonce"] = self.nonce
            self.actions[sid] = (obj, now)
        deadline = time.monotonic() + timeout_sec
        mid = str(obj.get("message_id") or "")
        while time.monotonic() < deadline:
            ack = self.ack_for(sid, mid)
            if ack:
                return ack
            time.sleep(0.01)
        return None
