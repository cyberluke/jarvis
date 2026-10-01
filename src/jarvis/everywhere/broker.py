"""Everywhere action broker: pipe server, snapshot registry, task queue.

One runtime, many surfaces. This broker owns the Everywhere plane's state:

* a current-user named-pipe server (``\\\\.\\pipe\\toastovac-everywhere-v1``)
  with the same SDDL construction as the terminal bridge (logon-SID-only
  DACL, reject-remote, message-mode);
* an immutable, bounded snapshot registry with revision supersession;
* an asynchronous task queue (``everywhere.tasks.TaskManager``): actions are
  queued and return a ``task_id`` immediately, a bounded worker pool runs the
  LLM with retry + cache, and ``task_event`` frames are pushed to subscribed
  hosts — the overlay never blocks on a slow model again;
* action execution through the canonical two-tier LLM router
  (``jarvis.llm``), never a second model client.

The native host owns Win32 specifics (hotkeys, UIA, overlays, OCR, input);
this broker owns intelligence, the queue and the transaction contract.
"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from ..debug import debug_log
from . import actions as action_table
from . import prompts as prompt_lib
from . import providers
from .model_profiles import resolve_model_for_action, resolve_profile_name
from .protocol import (
    ACTION_IDS,
    FAILURE_CODES,
    FRAME_HEADER_BYTES,
    MAX_PAYLOAD_BYTES,
    PIPE_NAME,
    PROTOCOL_ID,
    decode,
    encode,
    make_error,
    make_result,
    validate_inbound,
)
from .snapshots import SelectionSnapshot, text_hash
from .tasks import TaskManager

#: Bounded registry windows (no unbounded growth in a long-lived daemon).
_MAX_SNAPSHOTS = 32
_ACCEPT_TICK_MS = 50
# nDefaultTimeOut for the pipe instances (ms): bounded waits so a stalled
# peer cannot wedge an instance forever; the accept is released by
# DisconnectNamedPipe at stop() time.
_PIPE_TIMEOUT_MS = 2000

#: Per-frame pipe tracing (JARVIS_PIPE_TRACE=1). Ground truth for wedged
#: sessions: every ReadFile/WriteFile result with byte counts and error
#: codes, tagged by pipe instance index.
_PIPE_TRACE = os.environ.get("JARVIS_PIPE_TRACE") == "1"


def _trace(msg: str) -> None:
    if _PIPE_TRACE:
        print(f"[pipe-trace] {msg}", flush=True)

# Win32 constants for the message-mode pipe (same values the terminal
# bridge verified on this host family).
PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_MESSAGE = 0x00000004
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
ERROR_PIPE_CONNECTED = 535
ERROR_NO_DATA = 232
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_MORE_DATA = 234
ERROR_TIMEOUT = 6
_READ_BUF = 8192
_MAX_PIPE_INSTANCES = 8


def audio_device_options() -> dict:
    """Device lists for the overlay combos (sound input + output).

    Input lanes: ``loopback`` (system render loopback — what is playing on
    the machine) and ``mic`` (the engine's current capture device). Outputs
    are the WASAPI render endpoints as PortAudio indices — the same device
    space Piper's playback uses — with the current system default flagged.
    """
    inputs = [
        {"id": "loopback", "label": "System audio (loopback)"},
    ]
    mic_label = "Microphone (current device)"
    try:
        from .. import native_audio as _na
        for e in _na.enumerate_endpoints(2):
            if e.get("default_console") or e.get("default_communications"):
                name = str(e.get("friendly_name") or "").strip()
                if name:
                    mic_label = f"Microphone ({name})"
                break
    except Exception:
        pass
    inputs.append({"id": "mic", "label": mic_label})

    outputs: list[dict] = []
    default_out: Optional[int] = None
    try:
        import sounddevice as _sd  # type: ignore
        from ..output.audio_device import windows_default_output
        try:
            default_out = windows_default_output(_sd)
        except Exception:
            default_out = None
        hosts = _sd.query_hostapis()
        for i, info in enumerate(_sd.query_devices()):
            if int(info.get("max_output_channels", 0) or 0) < 1:
                continue
            try:
                host_name = str(
                    hosts[int(info.get("hostapi", 0))].get("name", ""))
            except Exception:
                host_name = ""
            if "WASAPI" not in host_name:
                continue
            outputs.append({
                "id": int(i),
                "label": str(info.get("name", f"device {i}")),
                "default": default_out is not None
                and int(i) == int(default_out),
            })
    except Exception:
        pass
    return {"inputs": inputs, "outputs": outputs}


class EverywhereBroker:
    """Named-pipe broker wiring the native host to the Jarvis runtime."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        raw_name = str(getattr(cfg, "everywhere_pipe_name", "")
                       or PIPE_NAME).strip()
        # CreateNamedPipeW (with explicit W bindings) needs the full form.
        prefix = "\\\\.\\pipe\\"
        if not raw_name.startswith(prefix):
            raw_name = prefix + raw_name
        self._pipe_name = raw_name
        self._k32 = ctypes.windll.kernel32
        self._adv = ctypes.windll.advapi32
        self._lock = threading.Lock()
        self._snapshots: Dict[str, SelectionSnapshot] = {}
        self._snapshot_order: list = []
        self._by_key: Dict[Any, str] = {}
        self._threads: list = []
        self._handles: list = []
        self._stop = threading.Event()
        self._started = False
        self.nonce = secrets.token_hex(8)
        # One warm OCR engine for the process lifetime (never per request).
        self._ocr_backend = None
        if str(getattr(cfg, "everywhere_ocr_backend", "")
               or "").strip().lower() == "oneocr":
            from ..vision.ocr.oneocr_backend import create_backend
            self._ocr_backend = create_backend(cfg)
        # Subtitles: buffered transcript lines + the lock guarding them.
        self._sub_lock = threading.Lock()
        self._sub_lines: list = []
        # Coach: buffered transcript/hint events (shares the sub lock).
        self._coach_events: list = []
        # LAN subtitle casting (off by default; toggled from the overlay).
        self._cast_enabled = False
        # Set by the daemon after construction: the live Whisper listener and
        # TTS engine the subtitles pipeline reuses.
        self._voice_listener = None
        self._tts_engine = None
        # Async task queue (submit -> task_id -> task_event pushes).
        self._tasks = TaskManager(cfg, on_event=self._on_task_event)
        # Subscribed (persistent) pipe clients: handle -> write lock. The host
        # subscribes once and receives task_event pushes until it disconnects.
        self._sub_lock = threading.Lock()
        self._subscribers: Dict[int, threading.Lock] = {}

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._bind()
        from ..terminal.bridge.named_pipe import _query_logon_sid
        sid = _query_logon_sid(self._k32, self._adv)
        sd_ptr = None
        if sid:
            sddl = f"D:P(A;;0xC0000000;;;{sid})"
            conv = self._adv.ConvertStringSecurityDescriptorToSecurityDescriptorW
            conv.argtypes = [ctypes.c_wchar_p, ctypes.c_uint,
                             ctypes.POINTER(ctypes.c_void_p),
                             ctypes.POINTER(ctypes.c_ulong)]
            conv.restype = ctypes.c_long
            out = ctypes.c_void_p()
            if conv(sddl, 1, ctypes.byref(out), None):
                sd_ptr = out.value

        class SA(ctypes.Structure):
            _fields_ = [("nLength", ctypes.c_uint),
                        ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", ctypes.c_int)]

        sa = SA(ctypes.sizeof(SA), sd_ptr, 0) if sd_ptr else None
        create = self._k32.CreateNamedPipeW
        create.restype = ctypes.c_void_p
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_uint, ctypes.c_void_p]
        for _ in range(_MAX_PIPE_INSTANCES):
            h = create(
                self._pipe_name,
                PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE
                | PIPE_REJECT_REMOTE_CLIENTS,
                _MAX_PIPE_INSTANCES, _READ_BUF, _READ_BUF,
                _PIPE_TIMEOUT_MS, ctypes.byref(sa) if sa else None,
            )
            if h:
                self._handles.append(h)
                _trace(f"create instance ok handle=0x{h & 0xffffffff:x}")
        if sd_ptr:
            self._k32.LocalFree(sd_ptr)
        if not self._handles:
            self._started = False
            debug_log("everywhere pipe not created (fail closed)",
                      "everywhere")
            return
        for i, h in enumerate(self._handles):
            t = threading.Thread(target=self._instance_loop, args=(h, i),
                                 name=f"everywhere-pipe-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        debug_log(
            f"everywhere pipe listening: {self._pipe_name} "
            f"(instances={len(self._handles)})", "everywhere")

    def stop(self) -> None:
        """Bounded shutdown: workers block in ConnectNamedPipe indefinitely
        (blocking mode), so each instance is woken with one dummy client
        connection; the worker sees the stop flag and exits before touching
        the handle. Handles close only after every worker exited (closing a
        handle with a pending ConnectNamedPipe would block)."""
        self._stop.set()
        self._tasks.shutdown()
        # Wake subscribed persistent readers by disconnecting them directly.
        # DisconnectNamedPipe waits for pending I/O on the handle to finish,
        # so the worker's blocking ReadFile must be cancelled first: with an
        # idle host (no frames in flight) that read never completes, and
        # stop() would wedge forever — hanging the desktop app's quit.
        with self._sub_lock:
            sub_handles = list(self._subscribers.keys())
            self._subscribers.clear()
        for h in sub_handles:
            try:
                self._k32.CancelIoEx(ctypes.c_void_p(h), None)
            except Exception:
                pass
            try:
                self._k32.DisconnectNamedPipe(ctypes.c_void_p(h))
            except Exception:
                pass
        create = self._k32.CreateFileW
        create.restype = ctypes.c_void_p
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p]
        for _ in range(len(self._handles)):
            dummy = create(self._pipe_name, 0xC0000000, 0, None, 3, 0, None)
            if dummy and dummy != ctypes.c_void_p(-1).value:
                self._k32.CloseHandle(ctypes.c_void_p(dummy))
        deadline = time.monotonic() + 2.0
        for t in self._threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        if not any(t.is_alive() for t in self._threads):
            for h in list(self._handles):
                handle = ctypes.c_void_p(h)
                try:
                    self._k32.DisconnectNamedPipe(handle)
                except Exception:
                    pass
                try:
                    self._k32.CloseHandle(handle)
                except Exception:
                    pass
            self._handles = []
        else:
            debug_log("everywhere broker stop: workers still alive "
                      "(handles left for process exit)", "everywhere")
        self._threads = []
        self._started = False

    # ── pipe I/O (message-mode, blocking with accept timeout) ─────────
    def _bind(self) -> None:
        """Explicit ctypes signatures: handles are void* (64-bit safe)."""
        k32 = self._k32
        ct = ctypes
        k32.ConnectNamedPipe.restype = ct.c_bool
        k32.ConnectNamedPipe.argtypes = [ct.c_void_p, ct.c_void_p]
        k32.ReadFile.restype = ct.c_bool
        k32.ReadFile.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_uint,
                                 ct.POINTER(ct.c_ulong), ct.c_void_p]
        k32.WriteFile.restype = ct.c_bool
        k32.WriteFile.argtypes = [ct.c_void_p, ct.c_void_p, ct.c_uint,
                                  ct.POINTER(ct.c_ulong), ct.c_void_p]
        k32.DisconnectNamedPipe.restype = ct.c_bool
        k32.DisconnectNamedPipe.argtypes = [ct.c_void_p]
        k32.CancelIoEx.restype = ct.c_bool
        k32.CancelIoEx.argtypes = [ct.c_void_p, ct.c_void_p]
        k32.PeekNamedPipe.restype = ct.c_bool
        k32.PeekNamedPipe.argtypes = [
            ct.c_void_p, ct.c_void_p, ct.c_uint,
            ct.POINTER(ct.c_ulong), ct.POINTER(ct.c_ulong),
            ct.POINTER(ct.c_ulong)]
        k32.CloseHandle.restype = ct.c_bool
        k32.CloseHandle.argtypes = [ct.c_void_p]
        k32.GetLastError.restype = ct.c_ulong
        k32.GetLastError.argtypes = []
        k32.LocalFree.restype = ct.c_void_p
        k32.LocalFree.argtypes = [ct.c_void_p]

    def _instance_loop(self, handle: int, index: int) -> None:
        """One connection per client; a ``subscribe`` frame upgrades the
        connection into a persistent push channel (the host's queue overlay
        subscription), everything else stays one-shot request/response."""
        k32 = self._k32
        h = ctypes.c_void_p(handle)
        while not self._stop.is_set():
            _trace(f"instance[{index}] ConnectNamedPipe waiting "
                   f"(handle=0x{handle & 0xffffffff:x})")
            ok = k32.ConnectNamedPipe(h, None)
            err = k32.GetLastError()
            _trace(f"instance[{index}] ConnectNamedPipe ok={ok} err={err}")
            if ok or err == ERROR_PIPE_CONNECTED:
                frame = self._read_message(h, tag=f"i{index}")
                debug_log(f"everywhere frame in: "
                          f"{'ok' if frame is not None else 'none'}",
                          "everywhere")
                if frame is None:
                    k32.DisconnectNamedPipe(h)
                    continue
                if str(frame.get("kind") or "") == "subscribe":
                    self._persistent_session(h, frame)
                    continue
                reply = self._dispatch(frame)
                debug_log(f"everywhere dispatched: "
                          f"{None if reply is None else reply.get('kind')}",
                          "everywhere")
                if reply is not None:
                    okw = self._write_message(h, reply)
                    debug_log(f"everywhere written: {okw}", "everywhere")
                k32.DisconnectNamedPipe(h)
                continue
            if err == ERROR_NO_DATA or err == ERROR_PIPE_NOT_CONNECTED:
                k32.DisconnectNamedPipe(h)
                continue
            if err == ERROR_TIMEOUT:
                continue
            return

    def _persistent_session(self, h, hello: dict) -> None:
        """Serve one subscribed client: reply with the current snapshot, then
        push task_event frames until the client disconnects or the broker
        stops. The reply to every subsequent request rides the same handle."""
        rid = str(hello.get("request_id") or "unknown")
        reply = self._dispatch(hello)
        if reply is not None:
            okw = self._write_message(h, reply)
            if not okw:
                debug_log("everywhere subscribe: ack write failed",
                          "everywhere")
                return
        wlock = threading.Lock()
        with self._sub_lock:
            self._subscribers[int(h.value)] = wlock
        debug_log(f"everywhere subscribed clients={len(self._subscribers)}",
                  "everywhere")
        try:
            while not self._stop.is_set():
                _trace(f"session[{int(h.value) & 0xffff:x}] waiting for frame")
                frame = self._read_message(h, tag=f"s{int(h.value) & 0xffff:x}")
                if frame is None:
                    break
                debug_log("everywhere frame in: ok (subscribed)",
                          "everywhere")
                r = self._dispatch(frame)
                if r is not None:
                    with wlock:
                        okw = self._write_message(h, r)
                    if not okw:
                        debug_log("everywhere subscribed: reply write failed",
                                  "everywhere")
                        break
        finally:
            with self._sub_lock:
                self._subscribers.pop(int(h.value), None)
            try:
                self._k32.DisconnectNamedPipe(h)
            except Exception:
                pass
            debug_log(f"everywhere unsubscribed clients={len(self._subscribers)}",
                      "everywhere")

    def _read_message(self, handle, tag: str = "") -> Optional[dict]:
        """Read one frame with PeekNamedPipe gating.

        The session thread runs this while OTHER threads (task manager) may
        WriteFile pushes on the same handle. On a message-mode pipe a
        long-pending ReadFile blocks concurrent writes on the handle (the
        observed wedge: pings/pushes never arrived while every write waited),
        so reads must never linger: peek for data, then read only when a
        frame is known to be waiting.
        """
        k32 = self._k32
        buf = ctypes.create_string_buffer(_READ_BUF)
        n = ctypes.c_ulong(0)
        data = bytearray()
        while True:
            avail = ctypes.c_ulong(0)
            pk = k32.PeekNamedPipe(handle, None, 0, None,
                                   ctypes.byref(avail), None)
            if not pk:
                _trace(f"peek[{tag}] failed err={k32.GetLastError()}")
                return None
            if avail.value == 0:
                time.sleep(0.005)
                continue
            ok = k32.ReadFile(handle, buf, _READ_BUF, ctypes.byref(n), None)
            err = 0 if ok else k32.GetLastError()
            _trace(f"read[{tag}] ok={ok} err={err} n={int(n.value)} "
                   f"total={len(data)}")
            if ok:
                data += buf.raw[: int(n.value)]
                if err != ERROR_MORE_DATA:
                    break
            elif err == ERROR_MORE_DATA:
                data += buf.raw[: _READ_BUF]
            else:
                return None
            if len(data) > MAX_PAYLOAD_BYTES + FRAME_HEADER_BYTES:
                return None
        obj, reason = decode(bytes(data))
        if obj is None:
            debug_log(f"everywhere frame rejected: {reason}", "everywhere")
        return obj

    def _write_message(self, handle, obj: dict) -> bool:
        """Write one frame; returns True only when WriteFile succeeded."""
        try:
            payload = encode(obj)
        except ValueError:
            debug_log("everywhere write dropped: message too large",
                      "everywhere")
            return False
        n = ctypes.c_ulong(0)
        buf = ctypes.create_string_buffer(payload, len(payload))
        ok = self._k32.WriteFile(handle, buf, len(payload),
                                 ctypes.byref(n), None)
        if not ok:
            err = self._k32.GetLastError()
            debug_log(
                f"everywhere write failed bytes={len(payload)} "
                f"error={err}", "everywhere")
        return bool(ok) and int(n.value) == len(payload)

    # ── push channel ──────────────────────────────────────────────────
    def _push_frame(self, frame: dict) -> None:
        """Broadcast one frame to all subscribed hosts."""
        try:
            data = encode(frame)
        except ValueError:
            return
        dead: list = []
        with self._sub_lock:
            subs = list(self._subscribers.items())
        for h, wlock in subs:
            with wlock:
                n = ctypes.c_ulong(0)
                buf = ctypes.create_string_buffer(data, len(data))
                ok = self._k32.WriteFile(
                    ctypes.c_void_p(h), buf, len(data), ctypes.byref(n), None)
                if not ok:
                    dead.append(h)
        if dead:
            with self._sub_lock:
                for h in dead:
                    self._subscribers.pop(h, None)
            debug_log(f"everywhere push dropped clients={len(dead)}",
                      "everywhere")

    def _on_task_event(self, payload: dict) -> None:
        """TaskManager callback: broadcast one task_event to all subscribers."""
        self._push_frame({"protocol": PROTOCOL_ID, "kind": "task_event",
                          **payload})

    def push_overlay_command(self, overlay: str) -> None:
        """Ask the subscribed host to toggle an overlay window (tray unhide).

        ``overlay`` is one of the host's action slots ("subtitles", "coach",
        "ocr"). The host shows the window; if it is already visible it stays
        put (idempotent from the tray's point of view).
        """
        overlay = str(overlay or "").strip().lower()
        if overlay not in ("subtitles", "coach", "ocr"):
            debug_log(f"everywhere push overlay: unknown {overlay!r}",
                      "everywhere")
            return
        debug_log(f"everywhere push overlay: {overlay}", "everywhere")
        self._push_frame({"protocol": PROTOCOL_ID, "kind": "overlay",
                          "overlay": overlay})

    # ── dispatch ──────────────────────────────────────────────────────
    def _dispatch(self, obj: dict) -> Optional[dict]:
        ok, reason = validate_inbound(obj)
        rid = str(obj.get("request_id") or "")
        if not ok:
            return make_error(rid or "unknown", "PROVIDER_UNAVAILABLE") \
                if rid else None
        kind = obj.get("kind")
        if kind == "ping":
            return make_result(rid, "pong", {"nonce": self.nonce})
        if kind == "subscribe":
            return self._on_subscribe(rid, obj)
        if kind == "snapshot":
            return self._on_snapshot(rid, obj)
        if kind == "action":
            return self._on_action(rid, obj)
        if kind == "apply":
            return self._on_apply(rid, obj)
        if kind == "cancel":
            return self._on_cancel(rid, obj)
        if kind == "task_cancel":
            return self._on_task_cancel(rid, obj)
        if kind == "task_result":
            return self._on_task_result(rid, obj)
        if kind == "task_list":
            return self._on_task_list(rid, obj)
        if kind == "subtitles":
            try:
                return self._on_subtitles(rid, obj)
            except Exception as exc:
                debug_log(f"everywhere subtitles dispatch error: "
                          f"{type(exc).__name__}: {exc}", "everywhere")
                return make_error(rid, "PROVIDER_UNAVAILABLE")
        if kind == "coach":
            try:
                return self._on_coach(rid, obj)
            except Exception as exc:
                debug_log(f"everywhere coach dispatch error: "
                          f"{type(exc).__name__}: {exc}", "everywhere")
                return make_error(rid, "PROVIDER_UNAVAILABLE")
        if kind == "ocr":
            try:
                return self._on_ocr(rid, obj)
            except Exception as exc:
                debug_log(f"everywhere ocr dispatch error: "
                          f"{type(exc).__name__}: {exc}", "everywhere")
                return make_error(rid, "PROVIDER_UNAVAILABLE")
        if kind == "video":
            try:
                return self._on_video(rid, obj)
            except Exception as exc:
                debug_log(f"everywhere video dispatch error: "
                          f"{type(exc).__name__}: {exc}", "everywhere")
                return make_error(rid, "PROVIDER_UNAVAILABLE")
        return make_error(rid, "PROVIDER_UNAVAILABLE")

    # ── async task plane ──────────────────────────────────────────────
    def _on_subscribe(self, rid: str, obj: dict) -> dict:
        """Host subscribes to the push channel; reply carries the current
        task snapshot so a reconnecting overlay rebuilds its queue."""
        return make_result(rid, "subscribed", {
            "nonce": self.nonce,
            "tasks": self._tasks.snapshot(),
            "server_time": time.time(),
        })

    def _on_action(self, rid: str, obj: dict) -> dict:
        """Queue one action; never blocks. Returns ``task_queued`` with the
        task id; progress arrives over the push channel as ``task_event``."""
        action = str(obj.get("action") or "")
        snapshot_id = str(obj.get("snapshot_id") or "")
        snap = self._snapshots.get(snapshot_id)
        if snap is None:
            return make_error(rid, "NO_SELECTION")
        if not snap.text:
            return make_error(rid, "EMPTY_SELECTION")
        if action not in ACTION_IDS:
            return make_error(rid, "PROVIDER_UNAVAILABLE")
        profile = resolve_profile_name(self._cfg, action)
        model = resolve_model_for_action(self._cfg, action)
        task = self._tasks.submit(
            action, snap, request_id=rid, profile=profile, model=model,
            target_language=str(obj.get("target_language") or "").strip(),
            prompt_id=str(obj.get("prompt_id") or ""))
        debug_log(
            f"everywhere.action.queued task={task.task_id} "
            f"action={task.action} profile={task.profile} model={task.model} "
            f"source={snap.source_kind}", "everywhere")
        summary = next(
            (s for s in self._tasks.snapshot(limit=64)
             if s.get("task_id") == task.task_id), {})
        return make_result(rid, "task_queued", {
            "task_id": task.task_id,
            "request_id": task.request_id,
            "action": task.action,
            "state": task.state,
            "model": task.model,
            "profile": task.profile,
            "position": summary.get("position", 0),
        })

    def _on_task_cancel(self, rid: str, obj: dict) -> dict:
        task_id = str(obj.get("task_id") or "")
        task = self._tasks.get(task_id)
        cancelled = self._tasks.cancel(task_id)
        debug_log(
            f"everywhere.task.cancel_request task={task_id} "
            f"cancelled={cancelled}", "everywhere")
        return make_result(rid, "task_cancel", {
            "task_id": task_id,
            "cancelled": cancelled,
            "state": task.state if task else "IDLE",
        })

    def _on_task_result(self, rid: str, obj: dict) -> dict:
        """Fetch the full result of one task (large results are not shipped
        inside the completed event; the overlay pulls them on demand)."""
        task = self._tasks.get(str(obj.get("task_id") or ""))
        if task is None:
            return make_error(rid, "TARGET_GONE")
        return make_result(rid, "task_result", {
            "task_id": task.task_id,
            "request_id": task.request_id,
            "action": task.action,
            "state": task.state,
            "failure": task.failure,
            "model": task.model,
            "profile": task.profile,
            "cache_hit": task.cache_hit,
            "result": task.result or "",
            "text": task.result or "",
            "translated_text": (task.result or "") if task.action == "translate" else "",
            "source_text": task.snapshot.text or "",
            "source_kind": task.snapshot.source_kind,
            "process_name": task.snapshot.process_name,
            "duration_ms": task.duration_ms,
            "first_token_ms": task.first_token_ms,
        })

    def _on_task_list(self, rid: str, obj: dict) -> dict:
        return make_result(rid, "task_list", {"tasks": self._tasks.snapshot()})

    # ── Video (own-player path: yt-dlp + HLS + reasoning) ─────────────
    def _on_video(self, rid: str, obj: dict) -> dict:
        """Voice control for the Toastovač video player.

        Commands (``command`` field):
          play      {video_id, transcode?}  -> download + stream to the TV player
          stop / pause / resume
          seek      {seconds}
          volume    {level: 0..1}
          status
          comments  {video_id}              -> top comments pushed to the player
          reactions {video_id}              -> stats + top comments
          summary   {video_id}              -> LLM summary of the transcript
          ask       {video_id, question}    -> LLM conversation about the video
          subtitles {mode: original|translated|off}
          style     {font, scale, color, bg} -> subtitle overlay styling
        """
        from . import video_server
        cmd = str(obj.get("command") or "")
        video_id = str(obj.get("video_id") or obj.get("id") or "")
        srv = video_server.get_video_server(self._cfg)
        if srv._httpd is None:
            if not srv.start():
                return make_error(rid, "PROVIDER_UNAVAILABLE")

        if cmd == "play":
            if not video_id:
                return make_error(rid, "PROVIDER_UNAVAILABLE")
            job = srv.play(video_id,
                           transcode=bool(obj.get("transcode", True)))
            video_server.push_player_command("play", {"video_id": video_id})
            self._tv_tap()
            return make_result(rid, "video_playing", job.snapshot())

        if cmd in ("stop", "pause", "resume"):
            video_server.push_player_command(cmd, {})
            return make_result(rid, f"video_{cmd}", {})

        if cmd == "seek":
            video_server.push_player_command("seek",
                                             {"seconds": float(obj.get("seconds") or 0)})
            return make_result(rid, "video_seek", {})

        if cmd == "volume":
            video_server.push_player_command(
                "volume", {"level": float(obj.get("level") or 1.0)})
            self._tv_tap()
            return make_result(rid, "video_volume", {})

        if cmd == "status":
            job = srv.active_job()
            return make_result(rid, "video_status",
                               {"job": job.snapshot() if job else None})

        if cmd == "comments":
            from . import yt_api
            items = yt_api.comments(video_id)
            video_server.push_player_command("comments", {"items": items})
            return make_result(rid, "video_comments", {"comments": items})

        if cmd == "reactions":
            from . import yt_api
            data = yt_api.top_reactions(video_id)
            video_server.push_player_command("comments",
                                             {"items": data.get("top_comments") or []})
            return make_result(rid, "video_reactions", data)

        if cmd == "summary":
            data = srv.summary(video_id)
            video_server.push_player_command("summary", data)
            return make_result(rid, "video_summary", data)

        if cmd == "ask":
            question = str(obj.get("question") or "")
            if not question:
                return make_error(rid, "PROVIDER_UNAVAILABLE")
            data = srv.ask(video_id, question)
            video_server.push_player_command(
                "ask", {"question": question, "answer": data.get("answer", "")})
            return make_result(rid, "video_ask", data)

        if cmd == "subtitles":
            mode = str(obj.get("mode") or "original")
            video_server.push_player_command("subtitles", {"mode": mode})
            return make_result(rid, "video_subtitles", {"mode": mode})

        if cmd == "style":
            style = {k: obj[k] for k in ("font", "scale", "color", "bg")
                     if k in obj}
            video_server.push_player_command("style", style)
            return make_result(rid, "video_style", style)

        return make_error(rid, "PROVIDER_UNAVAILABLE")

    # TV Bro's WebView pauses/unmutes playback only after a trusted click.
    # The voice path can't produce one, so fire a synthetic tap over adb —
    # fire-and-forget: if the TV/box is unreachable, playback still works
    # muted and the user can press OK on the remote.
    def _tv_tap(self) -> None:
        try:
            import shutil
            import subprocess
            import threading
            host = str(getattr(self._cfg, "tv_adb_host", "") or "192.168.1.122:5555")
            adb = shutil.which("adb")
            if not adb:
                return
            def _tap():
                try:
                    subprocess.run(
                        [adb, "-s", host, "shell", "input", "tap", "960", "540"],
                        capture_output=True, timeout=8,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:
                    pass
            threading.Thread(target=_tap, daemon=True).start()
        except Exception:
            pass

    # ── Screen Reading (OCR) with word-level quads ────────────────────
    def _on_ocr(self, rid: str, obj: dict) -> dict:
        """Alt+drag region OCR. The host sends a staged PNG path + the
        physical region; the warm OneOCR engine returns the text and the
        per-word quads (region-relative) so the overlay can render clickable
        word boxes. ``translate_to`` additionally translates the whole page.
        """
        backend = self._ocr_backend
        if backend is None:
            return make_error(rid, "OCR_BACKEND_UNAVAILABLE")
        image_path = str(obj.get("image_path") or "")
        if not image_path:
            return make_error(rid, "NO_SELECTION")
        try:
            from PIL import Image
            with Image.open(image_path) as img:
                doc = backend.recognize(
                    img,
                    language_hint=str(
                        getattr(self._cfg, "whisper_language", "") or ""),
                    rotation_hint=0,
                )
        except Exception as exc:
            debug_log(f"everywhere ocr failed: {exc}", "everywhere")
            return make_error(rid, "OCR_NO_TEXT")
        lines = []
        for line in doc.lines:
            lines.append({
                "text": line.text,
                "quad": [line.quad.x1, line.quad.y1, line.quad.x2,
                         line.quad.y2, line.quad.x3, line.quad.y3,
                         line.quad.x4, line.quad.y4] if line.quad else [],
                "words": [
                    {
                        "text": w.text,
                        "confidence": round(w.confidence, 3),
                        "quad": [w.quad.x1, w.quad.y1, w.quad.x2, w.quad.y2,
                                 w.quad.x3, w.quad.y3, w.quad.x4, w.quad.y4]
                        if w.quad else [],
                    }
                    for w in line.words
                ],
            })
        result = {
            "text": doc.text,
            "rotation_deg": doc.rotation_deg,
            "lines": lines,
            "line_count": len(doc.lines),
            "word_count": doc.word_count,
        }
        # Full-page translate: translate the whole recognized text.
        target = str(obj.get("translate_to") or "")
        if target and doc.text.strip():
            translated = self._subtitle_llm_chat(
                "You are a document translator. Translate the text into "
                f"{target}, preserving line breaks and layout. Output only "
                "the translation.", doc.text)
            result["translated_text"] = translated
            result["translated_to"] = target
        return make_result(rid, "ocr_result", result)

    # ── Interview / Meeting Coach ─────────────────────────────────────
    def _on_coach(self, rid: str, obj: dict) -> dict:
        """Control + poll channel for the coach overlay.

        Commands: start (answer_language, domain_hint), stop, poll, summarize.
        """
        from . import coach as coach_mod
        cmd = str(obj.get("command") or "")
        if cmd == "options":
            # Device lists for the overlay's sound input/output combos.
            return make_result(rid, "coach_options", {
                "audio": audio_device_options(),
            })
        if cmd == "start":
            service = coach_mod.ensure_coach(
                self._cfg,
                listener=getattr(self, "_voice_listener", None),
                llm_chat=self._subtitle_llm_chat,
                on_event=self._queue_coach_event)
            service.update_settings(
                answer_language=obj.get("answer_language"),
                domain_hint=obj.get("domain_hint"),
                audio_input=obj.get("audio_input"),
                audio_output=obj.get("audio_output"))
            if not service.start():
                return make_error(rid, "PROVIDER_UNAVAILABLE")
            return make_result(rid, "coach_started", {})
        if cmd == "stop":
            service = coach_mod.get_coach()
            if service is not None:
                service.stop()
            return make_result(rid, "coach_stopped", {})
        if cmd == "poll":
            events = self._drain_coach_events()
            service = coach_mod.get_coach()
            running = bool(service.running) if service is not None else False
            return make_result(rid, "coach_poll",
                               {"events": events, "running": running})
        if cmd == "summarize":
            service = coach_mod.get_coach()
            summary = service.summarize() if service is not None else ""
            return make_result(rid, "coach_summary", {"summary": summary})
        return make_error(rid, "PROVIDER_UNAVAILABLE")

    def _queue_coach_event(self, event: dict) -> None:
        with self._sub_lock:
            self._coach_events.append(event)
            if len(self._coach_events) > 100:
                del self._coach_events[:-100]
        kind = event.get("type")
        if kind in ("coach_session_started", "coach_session_ended"):
            # The meeting scribe switches the Voice PE pipeline to the
            # MEETING profile for the session and back afterwards
            # (see integrations/voice_pe/audio_pipeline.spec.md).
            try:
                from ..daemon import get_voice_pe_manager

                manager = get_voice_pe_manager()
                if manager is not None:
                    manager.set_meeting_mode(kind == "coach_session_started")
            except Exception as exc:
                debug_log(
                    f"broker: meeting-mode switch failed: {exc}", "everywhere"
                )

    def _drain_coach_events(self) -> list:
        with self._sub_lock:
            out = list(self._coach_events)
            self._coach_events.clear()
            return out

    # ── AI Subtitles (system-audio live translation) ──────────────────
    def _on_subtitles(self, rid: str, obj: dict) -> dict:
        """Control + poll channel for the subtitles overlay.

        Commands (``command`` field):
          options -> language lists with Piper badges + politeness tables
          start   -> begin capture (source/target/tts/politeness fields)
          stop    -> stop capture
          poll    -> drain the buffered transcript lines
        """
        from . import subtitles as subs
        from . import asian_context
        cmd = str(obj.get("command") or "")
        service = subs.get_service()
        if cmd == "options":
            from . import cast as cast_mod
            return make_result(rid, "subtitles_options", {
                "sources": subs.source_language_options(),
                "targets": subs.target_language_options(),
                "politeness": {
                    lang: asian_context.politeness_options(lang)
                    for lang in ("ko", "ja", "vi", "zh")
                },
                "audio": audio_device_options(),
                "loopback": self._loopback_available(),
                "cast_url": cast_mod.get_cast_server().url
                if getattr(self, "_cast_enabled", False) else "",
            })
        if cmd == "cast":
            # Toggle the LAN web-cast server on/off.
            from . import cast as cast_mod
            enable = bool(obj.get("enable", True))
            server = cast_mod.get_cast_server()
            if enable:
                if not server.start():
                    return make_error(rid, "PROVIDER_UNAVAILABLE")
                self._cast_enabled = True
                return make_result(rid, "subtitles_poll",
                                   {"lines": [], "running": True,
                                    "cast_url": server.url})
            server.stop()
            self._cast_enabled = False
            return make_result(rid, "subtitles_poll",
                               {"lines": [], "running": True, "cast_url": ""})
        if cmd == "start":
            service = self._ensure_subtitles_service()
            if service is None:
                return make_error(rid, "PROVIDER_UNAVAILABLE")
            service.update_settings(
                source=obj.get("source"), target=obj.get("target"),
                live_audio=obj.get("live_audio"),
                politeness=obj.get("politeness"),
                audio_input=obj.get("audio_input"),
                audio_output=obj.get("audio_output"))
            if not service.start():
                return make_error(rid, "PROVIDER_UNAVAILABLE")
            return make_result(rid, "subtitles_started", {})
        if cmd == "stop":
            if service is not None:
                service.stop()
            return make_result(rid, "subtitles_stopped", {})
        if cmd == "poll":
            lines = self._drain_subtitle_lines()
            running = bool(service.running) if service is not None else False
            return make_result(rid, "subtitles_poll",
                               {"lines": lines, "running": running})
        if cmd == "cast_devices":
            # Discover Google Cast devices on the LAN.
            from .cast_manager import get_manager
            devices = get_manager().discover_devices()
            return make_result(rid, "cast_devices", {"devices": devices})
        if cmd == "cast_start":
            # Start a cast session.
            from .cast_manager import get_manager, CastMode
            mode_str = str(obj.get("mode") or "lan_subtitles")
            try:
                mode = CastMode(mode_str)
            except ValueError:
                return make_error(rid, "PROVIDER_UNAVAILABLE")
            device = str(obj.get("device") or "") or None
            url = str(obj.get("url") or "")
            video_id = str(obj.get("video_id") or "")
            path = str(obj.get("path") or "")
            mgr = get_manager()
            mgr.set_subtitle_service(subs.get_service())
            ok = mgr.start_session(
                mode, device_name=device,
                url=url, video_id=video_id, path=path)
            if not ok:
                return make_error(rid, "PROVIDER_UNAVAILABLE")
            return make_result(rid, "cast_started", mgr.status)
        if cmd == "cast_stop":
            from .cast_manager import get_manager
            get_manager().stop_session()
            return make_result(rid, "cast_stopped", {})
        if cmd == "cast_status":
            from .cast_manager import get_manager
            return make_result(rid, "cast_status", get_manager().status)
        if cmd == "cast_subtitles":
            from .cast_manager import get_manager
            enable = bool(obj.get("enable", True))
            delay = int(obj.get("delay_ms") or 0)
            mgr = get_manager()
            if enable:
                mgr.set_subtitle_service(subs.get_service())
                if not mgr.start_subtitles(delay):
                    return make_error(rid, "PROVIDER_UNAVAILABLE")
            else:
                mgr.stop_subtitles()
            return make_result(rid, "cast_subtitles", {
                "enabled": enable, "delay_ms": delay})
        return make_error(rid, "PROVIDER_UNAVAILABLE")

    def _loopback_available(self) -> bool:
        # NOTE: do NOT call native_audio.capabilities() here — that is a live
        # call into the running audio engine and can block on the render-pump
        # lock, hanging the pipe worker. The DLL being loaded and the engine
        # having been created with loopback is a static fact; report whether
        # the engine module is loaded. (The capture loop re-checks per-frame.)
        try:
            from .. import native_audio
            return bool(native_audio.load())
        except Exception:
            return False

    def _ensure_subtitles_service(self):
        from . import subtitles as subs
        listener = getattr(self, "_voice_listener", None)
        service = subs.ensure_service(
            self._cfg,
            listener=listener,
            llm_chat=self._subtitle_llm_chat,
            tts=getattr(self, "_tts_engine", None),
            on_line=self._queue_subtitle_line)
        return service

    def _subtitle_llm_chat(self, system: str, user: str) -> str:
        """Translate call routed through the existing LLM backend (local)."""
        from ..reply.engine import chat_with_messages
        resp = chat_with_messages(
            self._cfg,
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            timeout_sec=60.0)
        if isinstance(resp, dict):
            msg = resp.get("message") or {}
            if isinstance(msg, dict):
                return str(msg.get("content") or "")
        return ""

    # Buffered transcript lines waiting for the overlay's poll.
    def _queue_subtitle_line(self, line: dict) -> None:
        with self._sub_lock:
            self._sub_lines.append(line)
            if len(self._sub_lines) > 50:
                del self._sub_lines[:-50]
        # Also mirror to the LAN web-cast page when casting is on.
        if getattr(self, "_cast_enabled", False):
            try:
                from . import cast as cast_mod
                text = line.get("translated_text") or line.get("source_text") or ""
                cast_mod.push_subtitle(text)
            except Exception:
                pass

    def _drain_subtitle_lines(self) -> list:
        with self._sub_lock:
            out = list(self._sub_lines)
            self._sub_lines.clear()
            return out

    # ── snapshots ─────────────────────────────────────────────────────
    def _on_snapshot(self, rid: str, obj: dict) -> dict:
        raw = obj.get("snapshot") or {}
        try:
            snap = SelectionSnapshot.from_dict(raw)
        except ValueError as exc:
            debug_log(f"everywhere snapshot invalid: {exc}", "everywhere")
            return make_error(rid, "NO_SELECTION")
        if not snap.snapshot_id:
            return make_error(rid, "NO_SELECTION")
        if snap.source_kind == "ocr-region" and not snap.text:
            snap = self._ocr_fill(snap)
        if not snap.text:
            return make_error(rid, "EMPTY_SELECTION")
        key = (snap.provider_id, snap.hwnd, snap.process_id,
               snap.source_kind)
        with self._lock:
            prev = self._by_key.get(key)
            if prev and prev != snap.snapshot_id:
                old = self._snapshots.get(prev)
                if old is not None and snap.revision and \
                        snap.revision > old.revision:
                    self._supersede_locked(prev)
            self._snapshots[snap.snapshot_id] = snap
            self._snapshot_order.append(snap.snapshot_id)
            self._by_key[key] = snap.snapshot_id
            while len(self._snapshot_order) > _MAX_SNAPSHOTS:
                drop = self._snapshot_order.pop(0)
                old = self._snapshots.pop(drop, None)
                if old is not None:
                    k = (old.provider_id, old.hwnd, old.process_id,
                         old.source_kind)
                    if self._by_key.get(k) == drop:
                        self._by_key.pop(k, None)
        debug_log(
            "everywhere.snapshot.created "
            f"provider={snap.provider_id} source={snap.source_kind} "
            f"proc={snap.process_name} len={snap.text_length} "
            f"hash={snap.text_sha[:12]}", "everywhere")
        payload = {
            "snapshot_id": snap.snapshot_id,
            "revision": snap.revision,
            "text_hash": snap.text_sha,
        }
        if snap.source_kind == "ocr-region":
            # The native host renders the OCR result straight from the
            # registration reply; word geometry survives in the snapshot.
            payload["text"] = snap.text
            sem = snap.semantic_context or {}
            ocr = sem.get("ocr") if isinstance(sem, dict) else None
            if isinstance(ocr, dict):
                payload["word_count"] = sum(
                    len(line.get("words") or ())
                    for line in ocr.get("lines") or ())
        return make_result(rid, "action_result", payload)

    def _supersede_locked(self, snapshot_id: str) -> None:
        """Mark transactions of an old revision STALE (automatic insertion
        is refused; the queue UI still shows the finished result)."""
        debug_log(
            f"everywhere.target.stale snapshot={snapshot_id}", "everywhere")

    # ── OCR region fill (Alt+drag, oneocr backend) ────────────────────
    def _ocr_fill(self, snap: SelectionSnapshot) -> SelectionSnapshot:
        """Recognize a staged screen-region PNG into the snapshot.

        The native host captures the region, writes a PNG, and sends the
        snapshot with an empty text plus ``provider_token.image_path``.
        The warm OneOCR engine fills text, line geometry and per-word
        quads/confidences; the image buffer is discarded afterwards.
        """
        tok = dict(snap.provider_token or {})
        if str(tok.get("ocr_backend") or "") != "oneocr":
            return snap
        backend = self._ocr_backend
        image_path = str(tok.get("image_path") or "")
        if backend is None or not image_path:
            return snap
        try:
            from PIL import Image
            with Image.open(image_path) as img:
                doc = backend.recognize(
                    img,
                    language_hint=str(
                        getattr(self._cfg, "whisper_language", "") or ""),
                    rotation_hint=0,
                )
        except RuntimeError as exc:
            debug_log(f"everywhere.ocr.failed reason={exc}", "everywhere")
            return snap
        except Exception as exc:
            debug_log(f"everywhere.ocr.failed "
                      f"reason={type(exc).__name__}", "everywhere")
            return snap
        try:
            from dataclasses import replace
            bounds = tuple(
                line.quad.as_rect() for line in doc.lines if line.quad)
            sem = dict(snap.semantic_context or {})
            sem["ocr"] = {
                "backend": doc.backend,
                "rotation_deg": doc.rotation_deg,
                "lines": [
                    {
                        "text": line.text,
                        "quad": [line.quad.x1, line.quad.y1, line.quad.x2,
                                 line.quad.y2, line.quad.x3, line.quad.y3,
                                 line.quad.x4, line.quad.y4]
                        if line.quad else [],
                        "words": [
                            {
                                "text": word.text,
                                "confidence": word.confidence,
                                "quad": [word.quad.x1, word.quad.y1,
                                         word.quad.x2, word.quad.y2,
                                         word.quad.x3, word.quad.y3,
                                         word.quad.x4, word.quad.y4]
                                if word.quad else [],
                            }
                            for word in line.words
                        ],
                    }
                    for line in doc.lines
                ],
            }
            sha = text_hash(doc.text)
            tok["recognized_text_hash"] = sha
            debug_log(
                f"everywhere.ocr.completed backend={doc.backend} "
                f"lines={len(doc.lines)} words={doc.word_count}",
                "everywhere")
            return replace(snap, text=doc.text, text_sha=sha,
                           selection_bounds=bounds,
                           semantic_context=sem, provider_token=tok)
        except Exception as exc:  # pragma: no cover — defensive
            debug_log(f"everywhere.ocr.failed "
                      f"reason={type(exc).__name__}", "everywhere")
            return snap

    # ── actions ───────────────────────────────────────────────────────
    def _on_action(self, rid: str, obj: dict) -> dict:
        action = str(obj.get("action") or "")
        snapshot_id = str(obj.get("snapshot_id") or "")
        snap = self._snapshots.get(snapshot_id)
        if snap is None:
            return make_error(rid, "NO_SELECTION")
        if not snap.text:
            return make_error(rid, "EMPTY_SELECTION")
        profile = resolve_profile_name(self._cfg, action)
        model = resolve_model_for_action(self._cfg, action)
        txn = _Txn(rid, action, snapshot_id, profile, model)
        # An explicit target_language from the host (the panel picker) wins.
        _tgt = str(obj.get("target_language") or "").strip()
        if _tgt:
            txn.target_language_override = _tgt
        with self._lock:
            self._txns[rid] = txn
            while len(self._txns) > _MAX_TRANSACTIONS:
                oldest = next(iter(self._txns))
                if self._txns[oldest].final:
                    self._txns.pop(oldest, None)
                else:
                    break
        if action in ACTION_IDS:
            self._execute(txn, snap,
                          prompt_id=str(obj.get("prompt_id") or ""))
        else:
            return make_error(rid, "PROVIDER_UNAVAILABLE")
        return self._result_payload(rid, txn)

    def _result_payload(self, rid: str, txn: _Txn) -> dict:
        if txn.state == "DONE":
            return make_result(rid, "action_result", {
                "state": txn.state,
                "action": txn.action,
                "snapshot_id": txn.snapshot_id,
                "text_hash": (self._snapshots.get(txn.snapshot_id)
                              or SelectionSnapshot(
                                  "", 0, 0.0, "uia-text", "", "", "",
                              )).text_sha,
                "model_profile": txn.model_profile,
                "model": txn.model,
                "result": txn.result or "",
                "duration_ms": int((time.monotonic() - txn.started) * 1000),
                "first_token_ms": (
                    int((txn.first_token_at - txn.started) * 1000)
                    if txn.first_token_at else None),
            })
        return make_error(rid, txn.failure or "MODEL_UNAVAILABLE")

    def _execute(self, txn: _Txn, snap: SelectionSnapshot, *,
                 prompt_id: str = "") -> None:
        from ..llm import get_llm_backend

        cfg = self._cfg
        backend = get_llm_backend(cfg)
        model = txn.model
        if not model or backend is None:
            txn.state = "CANCELLED"
            txn.failure = "MODEL_UNAVAILABLE"
            return
        txn.state = "ACTION_PENDING"
        fast = txn.model_profile in ("fast-edit", "structured-edit")
        timeout = float(getattr(
            cfg, "llm_digest_timeout_sec" if fast else "llm_chat_timeout_sec",
            12.0 if fast else 180.0))
        system = prompt_lib.get_action_prompt(cfg, txn.action)
        sem = dict(snap.semantic_context or {})
        if snap.source_kind == "vscode-editor" and snap.provider_token.get(
                "languageId"):
            sem.setdefault("language_id",
                           str(snap.provider_token["languageId"]))
        if snap.source_kind in action_table.TERMINAL_SOURCE_KINDS:
            for key in ("shell", "remote_kind", "remote_authority", "cwd"):
                if snap.provider_token.get(key):
                    sem.setdefault(key, str(snap.provider_token[key]))
        snap_dict = snap.to_dict()
        snap_dict["semantic_context"] = sem
        target_language = None
        count = None
        if txn.action == "translate":
            # An explicit target_language from the host (the panel's picker)
            # wins over the configured default.
            target_language = str(
                txn.target_language_override
                or getattr(cfg, "everywhere_translate_default_language", "cs")
                or "cs")
            system = system.replace("{target_language}", target_language)
        if txn.action == "alternatives":
            count = action_table.alternatives_count(cfg)
        user = prompt_lib.build_user_block(
            snap_dict, target_language=target_language,
            alternatives_count=count)
        if txn.action == "prompt" and prompt_id:
            entry = self._lookup_prompt(prompt_id)
            if entry is None:
                txn.state = "CANCELLED"
                txn.failure = "PROVIDER_UNAVAILABLE"
                return
            system = str(entry.get("instruction") or system)
            model = model or resolve_model_for_action(cfg, "prompt")
            prompt_lib.touch_usage(
                list(getattr(cfg, "everywhere_prompt_library", []) or []),
                prompt_id)
        debug_log(
            f"everywhere.action.started action={txn.action} "
            f"profile={txn.model_profile} model={model} "
            f"source={snap.source_kind}", "everywhere")
        txn.state = "STREAMING"
        buffer: list = []

        def _on_token(chunk: str) -> None:
            if txn.cancel.is_set():
                raise _Cancelled()
            if txn.first_token_at is None:
                txn.first_token_at = time.monotonic()
                debug_log(
                    f"everywhere.action.first_token request={txn.request_id}",
                    "everywhere")
            buffer.append(chunk)

        # Fast-tier actions (translate, rewrite, proofread, alternatives) run
        # without chain-of-thought so they answer straight away; the reasoning
        # profile keeps thinking on. gemma4/llama.cpp honours
        # chat_template_kwargs.enable_thinking=False.
        wants_thinking = txn.model_profile not in (
            "fast-edit", "structured-edit", "creative-edit")
        try:
            full = backend.streaming(
                model, system, user, on_token=_on_token,
                timeout_sec=timeout, thinking=wants_thinking)
            if txn.cancel.is_set():
                raise _Cancelled()
            txn.result = full if full is not None else "".join(buffer)
            if txn.result is None:
                txn.state = "CANCELLED"
                txn.failure = "MODEL_UNAVAILABLE"
                return
            if txn.action == "proofread":
                parsed, reason = proofread_lib.parse_proofread(
                    txn.result, source_hash=snap.text_sha)
                if parsed is None:
                    txn.state = "CANCELLED"
                    txn.failure = ("MODEL_UNAVAILABLE"
                                   if reason == "empty" else "PROVIDER_UNAVAILABLE")
                    return
                txn.result = json.dumps(parsed, ensure_ascii=False)
            txn.state = "RESULT_READY"
            txn.state = "DONE"
            debug_log(
                f"everywhere.action.completed request={txn.request_id} "
                f"result_length={len(txn.result or '')}", "everywhere")
        except _Cancelled:
            txn.state = "CANCELLED"
            txn.failure = "MODEL_CANCELLED"
            debug_log(
                f"everywhere.action.cancelled request={txn.request_id}",
                "everywhere")
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            txn.state = "CANCELLED"
            txn.failure = "MODEL_UNAVAILABLE"
            debug_log(
                f"everywhere.action.failed request={txn.request_id} "
                f"error={exc}", "everywhere")

    def _lookup_prompt(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        for entry in getattr(self._cfg, "everywhere_prompt_library", []) or []:
            if isinstance(entry, dict) and entry.get("id") == prompt_id:
                return entry
        return None

    # ── apply ─────────────────────────────────────────────────────────
    def _on_apply(self, rid: str, obj: dict) -> dict:
        snapshot_id = str(obj.get("snapshot_id") or "")
        task_ref = str(obj.get("task_id")
                       or obj.get("target_request_id") or "")
        snap = self._snapshots.get(snapshot_id)
        task = self._tasks.get(task_ref) if task_ref else None
        if snap is None or task is None:
            return make_error(rid, "TARGET_GONE")
        if task.state != "completed":
            return make_error(rid, "MODEL_UNAVAILABLE")
        current = obj.get("current") if isinstance(obj.get("current"), dict) \
            else snap.to_dict()
        ok, failure = providers.revalidate(snap.to_dict(), current)
        debug_log(
            f"everywhere.apply.started task={task.task_id} "
            f"capability={snap.replace_capability}", "everywhere")
        if not ok:
            debug_log(
                f"everywhere.apply.failed task={task.task_id} "
                f"failure={failure}", "everywhere")
            return make_error(rid, failure if failure in FAILURE_CODES
                              else "TARGET_CHANGED")
        debug_log(
            f"everywhere.apply.completed task={task.task_id} "
            f"result_length={len(task.result or '')}", "everywhere")
        return make_result(rid, "apply_result", {
            "state": "APPLIED",
            "snapshot_id": snapshot_id,
            "task_id": task.task_id,
            "replace_capability": snap.replace_capability,
            "result": task.result or "",
        })

    def _on_cancel(self, rid: str, obj: dict) -> dict:
        """Legacy one-shot cancel (kept for old clients); task_cancel is the
        canonical path now."""
        target = str(obj.get("target_request_id") or "")
        cancelled = self._tasks.cancel(target) if target else False
        task = self._tasks.get(target)
        debug_log(f"everywhere.task.cancel_request task={target} "
                  f"cancelled={cancelled}", "everywhere")
        return make_result(rid, "task_cancel", {
            "task_id": target,
            "cancelled": cancelled,
            "state": task.state if task else "IDLE",
        })

    # ── voice surface ─────────────────────────────────────────────────
    def handle_voice_utterance(self, text: str) -> Optional[str]:
        """Deterministic voice route: same snapshot, same action ids.

        Returns the short result line, or ``None`` when this utterance is
        not an Everywhere action (the reply engine then continues normally).
        """
        folded = _fold(text)
        parts = folded.split()
        if parts and parts[0].strip(".,;") in (
                "toustovac", "toastovac", "toaster"):
            parts = parts[1:]
        if not parts:
            return None
        snap = self._latest_snapshot()
        verb = parts[0].rstrip(".,;")
        mapping = {
            "prepis": "rewrite",
            "oprav": "proofread",
            "vysvetli": "explain",
            "preloz": "translate",
            "varianty": "alternatives",
        }
        action = mapping.get(verb)
        prompt_id = ""
        if action is None:
            for entry in getattr(self._cfg, "everywhere_prompt_library",
                                 []) or []:
                if isinstance(entry, dict) and \
                        _fold(str(entry.get("name") or "")) == folded:
                    action = "prompt"
                    prompt_id = str(entry.get("id") or "")
                    break
            if action is None:
                return None
        if snap is None:
            return "Nemám žádný aktuální výběr."
        rid = f"v{secrets.token_hex(4)}"
        task = self._tasks.submit(
            action, snap, request_id=rid,
            profile=resolve_profile_name(self._cfg, action),
            model=resolve_model_for_action(self._cfg, action),
            prompt_id=prompt_id)
        task.done_event.wait(timeout=180.0)
        if task.state == "completed":
            if task.result_mode == "panel":
                # Panel-mode results (translate, explain) are displayed, not
                # spoken. The full result is available via the pipe.
                return "Hotovo. Výsledek je v panelu."
            return task.result or ""
        if task.state == "failed":
            return "Akce nedopadla."
        return "Akce byla zrušena."

    def _latest_snapshot(self) -> Optional[SelectionSnapshot]:
        with self._lock:
            for sid in reversed(self._snapshot_order):
                snap = self._snapshots.get(sid)
                if snap is not None:
                    return snap
        return None

    # ── diagnostics ───────────────────────────────────────────────────
    def health(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": bool(getattr(self._cfg, "everywhere_enabled",
                                        False)),
                "pipe": self._pipe_name,
                "instances": len(self._handles),
                "snapshots": len(self._snapshots),
                "subscribers": len(self._subscribers),
                "tasks": len(self._tasks.snapshot()),
            }


def _fold(text: str) -> str:
    import unicodedata
    dec = unicodedata.normalize("NFKD", str(text or "").casefold())
    return " ".join("".join(
        ch for ch in dec if not unicodedata.combining(ch)).split())
