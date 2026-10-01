#!/usr/bin/env python3
"""Isolated faster-whisper STT worker for the voice pipeline.

The listener runs the Whisper model in this dedicated process so the
ctranslate2 inference and its Python glue never compete with the daemon's
other threads (Everywhere broker, pipe workers, UI, TTS) for the GIL.
Without this isolation a busy voice pipeline (continuous STT on noise)
starves the Everywhere broker for tens of seconds.

Transport mirrors ``openvino_runtime.OVSpeechWorker``: JSON lines over
stdin/stdout, base64 float32 audio, request/response matched by ``id``,
stage lines without an ``id`` forwarded to the runtime log. stderr is
drained by the owner so the pipes can never deadlock.

Spawned by ``SttWorkerClient``:
  * dev:        ``python -m jarvis.listening.fasterwhisper_worker``
                with ``PYTHONPATH`` pointing at ``src``;
  * frozen:     re-exec of the bundle with ``JARVIS_STT_WORKER=1``
                (``desktop_app.app.main`` routes to this module).
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# ── environment contract (worker spawn) ────────────────────────────────
ENV_MODEL = "JARVIS_STT_MODEL"
ENV_CACHE = "JARVIS_STT_CACHE_DIR"
ENV_DEVICE = "JARVIS_STT_DEVICE"
ENV_COMPUTE = "JARVIS_STT_COMPUTE"
ENV_LANGUAGE = "JARVIS_STT_LANGUAGE"
ENV_THREADS = "JARVIS_STT_CPU_THREADS"
ENV_FROZEN_MARKER = "JARVIS_STT_WORKER"

#: The decode options the pipeline wants on faster-whisper: the clip is
#: already VAD-trimmed by the outer grid, each utterance is self-contained,
#: and only ``text`` / ``avg_logprob`` / ``no_speech_prob`` are read out of
#: the segments. ``suppress_tokens=[-1]`` is the non-speech marker set
#: (``(mrmusic)`` etc.).
FASTER_WHISPER_TRANSCRIBE_KWARGS: Dict[str, Any] = {
    "vad_filter": False,
    "condition_on_previous_text": False,
    "without_timestamps": True,
    "suppress_tokens": [-1],
}

#: ISO-639-1 codes the pipeline can force per decode. ``"cs+vi"`` is NOT a
#: language code: it is a closed-set selector the listener resolves into two
#: forced passes (``["cs", "vi"]``) before calling the worker, so the worker
#: only ever sees a single code or ``None`` (auto-detect). Passing the raw
#: selector into ``WhisperModel.transcribe`` raises a ValueError.
_SINGLE_LANGUAGE_CODES = ("en", "cs", "vi", "sk")


def _resolve_forced_language(selector: Optional[str]) -> Optional[str]:
    """Single ISO code from a selector, or ``None`` for auto-detect.

    A configured single code is the forced language (lifts transcript
    precision); ``"cs+vi"`` (and anything unrecognized) resolves to
    ``None`` — the caller runs the closed-set multiselect as per-code
    forced passes, so the worker must never pass the raw selector through.
    """
    if not selector:
        return None
    code = str(selector).strip().lower()
    return code if code in _SINGLE_LANGUAGE_CODES else None


def worker_env_for_cfg(cfg: Any) -> Dict[str, str]:
    """Worker configuration serialized from the daemon's config object."""
    env = {
        ENV_MODEL: str(getattr(cfg, "whisper_model", "small") or "small"),
        ENV_CACHE: str(getattr(cfg, "whisper_cache_dir", "") or ""),
        ENV_DEVICE: str(getattr(cfg, "whisper_device", "auto") or "auto"),
        ENV_COMPUTE: str(getattr(cfg, "whisper_compute_type", "int8") or "int8"),
        ENV_LANGUAGE: str(getattr(cfg, "whisper_language", "") or ""),
        ENV_THREADS: str(os.cpu_count() or 4),
    }
    return env


def _resolve_transcribe_kwargs(entry_point, preferred: dict) -> tuple[dict, list]:
    """Keep only the ``preferred`` keys the installed entry point accepts."""
    accepted: dict = {}
    rejected: list = []
    try:
        import inspect

        params = inspect.signature(entry_point).parameters
        accepts_extra = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except Exception:  # pragma: no cover - signature introspection unavailable
        params = {}
        accepts_extra = True
    for key, value in preferred.items():
        if accepts_extra or key in params:
            accepted[key] = value
        else:
            rejected.append(key)
    return accepted, rejected


def _is_turbo_supported() -> bool:
    """Check if the installed faster-whisper supports large-v3-turbo."""
    try:
        import faster_whisper
        from packaging.version import Version

        return Version(faster_whisper.__version__) >= Version("1.1.0")
    except Exception:
        return False


def _clear_corrupted_cache(error_message: str) -> bool:
    """Delete the ``models--`` directory named by a CTranslate2 open error."""
    import re
    import shutil

    match = re.search(
        r"unable to open file\s+'[^']+'\s+in model\s+'([^']+)'",
        error_message,
        re.IGNORECASE,
    )
    if not match:
        return False
    snapshot_path = match.group(1)
    from pathlib import Path

    path = Path(snapshot_path)
    model_dir = None
    for parent in [path] + list(path.parents):
        if parent.name.startswith("models--"):
            model_dir = parent
            break
    if model_dir is None or not model_dir.is_dir():
        return False
    try:
        shutil.rmtree(model_dir)
        return True
    except OSError:
        return False


def _resolve_local(path_root: str, size_name: str) -> str:
    """Snapshot folder that contains model.bin, or ``""`` (hub download)."""
    try:
        import os as _os

        if not _os.path.isdir(path_root):
            return ""
        for folder in sorted(_os.listdir(path_root)):
            if not folder.startswith("models--") or not folder.endswith("--" + size_name):
                continue
            snaps = _os.path.join(path_root, folder, "snapshots")
            if not _os.path.isdir(snaps):
                continue
            for snap in sorted(_os.listdir(snaps), reverse=True):
                model_bin = _os.path.join(snaps, snap, "model.bin")
                if _os.path.isfile(model_bin) and _os.path.getsize(model_bin) > 0:
                    return _os.path.join(snaps, snap)
    except Exception:
        return ""
    return ""


def _setup_nvidia_dll_path() -> None:
    """Register NVIDIA CUDA DLL directories before the ctypes probe.

    Ported from the listener's ``_setup_nvidia_dll_path`` (the wheels were
    solved there once; the probe in this worker must not regress to
    ``CUDA libraries missing`` on machines where CUDA is installed):

    * pip wheels ``nvidia-cublas-cu12`` / ``nvidia-cudnn-cu12`` install DLLs
      under ``nvidia/<pkg>/bin`` — ``nvidia.cublas.__path__`` is the shared
      namespace root, so the subdirectories are scanned for ``bin`` folders
      (in dev and in the PyInstaller bundle alike);
    * the CTranslate2 wheel ships its own cuDNN next to ``ctranslate2.dll``;
    * a system CUDA Toolkit has its own ``bin`` / ``bin\\x64`` folders;
    * a frozen installer places them in ``{app}/cuda``.

    Both ``os.add_dll_directory`` (for ``ctypes.CDLL``) and PATH (for child
    processes) are updated; on Windows a PATH change after process start
    does not affect ``ctypes.CDLL`` search without ``add_dll_directory``.
    """
    import os

    dirs_to_add: list = []

    # 1. NVIDIA pip wheels / bundled nvidia namespace: scan every ``bin``
    # folder under the namespace root's subpackages (cublas, cudnn, ...).
    nvidia_roots: list = []
    try:
        import nvidia  # type: ignore[import-untyped]

        nvidia_roots.extend(str(p) for p in nvidia.__path__)
    except (ImportError, AttributeError):
        pass
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None) or os.path.dirname(
            sys.executable)
        nvidia_roots.append(os.path.join(meipass, "nvidia"))
    for root in nvidia_roots:
        try:
            subdirs = [e.name for e in os.scandir(root) if e.is_dir()]
        except OSError:
            continue
        for sub in subdirs:
            bin_dir = os.path.join(root, sub, "bin")
            if os.path.isdir(bin_dir):
                dirs_to_add.append(bin_dir)

    # 2. CTranslate2 wheel: ships its own cuDNN next to ctranslate2.dll.
    try:
        import ctranslate2  # type: ignore[import-untyped]

        ct2_dir = os.path.dirname(str(ctranslate2.__file__))
        if os.path.isdir(ct2_dir):
            dirs_to_add.append(ct2_dir)
    except (ImportError, AttributeError, TypeError):
        pass

    # 3. System CUDA Toolkit: ``...\CUDA\v<ver>\bin`` and its ``x64``
    # sibling, newest version first.
    for toolkit_root in (
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA",
        r"C:\NVIDIA",
    ):
        if not os.path.isdir(toolkit_root):
            continue
        try:
            versions = sorted(
                (e.name for e in os.scandir(toolkit_root) if e.is_dir()),
                reverse=True,
            )
        except OSError:
            continue
        for version in versions:
            for leaf in ("bin", os.path.join("bin", "x64")):
                candidate = os.path.join(toolkit_root, version, leaf)
                if os.path.isdir(candidate):
                    dirs_to_add.append(candidate)

    # 4. Frozen installer layout: {app}/cuda (install_cuda.ps1).
    if getattr(sys, "frozen", False):
        cuda_dir = os.path.join(os.path.dirname(sys.executable), "cuda")
        if os.path.isdir(cuda_dir):
            dirs_to_add.append(cuda_dir)

    if dirs_to_add:
        seen: set = set()
        unique = [d for d in dirs_to_add
                  if not (d in seen or seen.add(d))]
        os.environ["PATH"] = (
            os.pathsep.join(unique) + os.pathsep +
            os.environ.get("PATH", ""))
        for d in unique:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            _say(f"  ℹ️  NVIDIA DLL path: {d}")


def _probe_cuda_available() -> tuple[bool, list]:
    """Probe cuBLAS + cuDNN availability once per process (DLL scan)."""
    _setup_nvidia_dll_path()
    missing_libs: list = []
    cublas_found = False
    cudnn_found = False
    try:
        import ctypes

        for ver in range(20, 10, -1):
            try:
                ctypes.CDLL(f"cublas64_{ver}.dll")
                cublas_found = True
                break
            except OSError:
                continue
        if not cublas_found:
            missing_libs.append("cuBLAS")
        for ver in range(15, 7, -1):
            for stem in ("cudnn_ops64_", "cudnn64_"):
                name = f"{stem}{ver}.dll"
                try:
                    ctypes.CDLL(name)
                    cudnn_found = True
                    break
                except OSError:
                    continue
            if cudnn_found:
                break
        if not cudnn_found:
            missing_libs.append("cuDNN")
    except Exception:
        pass
    return cublas_found and cudnn_found, missing_libs


def _probe_windows_cuda_libraries(device: str) -> tuple[str, list]:
    """Return the device to use and any missing CUDA lib names."""
    if sys.platform != "win32" or device not in ("auto", "cuda"):
        return device, []
    available, missing_libs = _probe_cuda_available()
    if not available:
        return "cpu", missing_libs
    return device, []


# ── payload encoding ───────────────────────────────────────────────────
def _audio_to_b64(audio: Any) -> Tuple[str, int]:
    """float32 samples -> (base64, sample count)."""
    import numpy as np

    arr = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).ravel())
    return base64.b64encode(arr.tobytes()).decode("ascii"), int(arr.size)


def _audio_from_b64(b64: str) -> Any:
    import numpy as np

    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


# ── result proxies (attribute surface the listener/dictation read) ─────
class SegmentRow:
    """faster-whisper segment surface over the wire (``seg.text`` etc.)."""

    __slots__ = ("text", "avg_logprob", "no_speech_prob", "start", "end",
                 "kv_cache_exhausted")

    def __init__(self, d: dict) -> None:
        self.text: str = str(d.get("text") or "")
        self.avg_logprob: Optional[float] = d.get("avg_logprob")
        self.no_speech_prob: Optional[float] = d.get("no_speech_prob")
        self.start: float = float(d.get("start") or 0.0)
        self.end: float = float(d.get("end") or 0.0)
        self.kv_cache_exhausted: bool = bool(d.get("kv_cache_exhausted") or False)


class InfoRow:
    """TranscriptionInfo surface over the wire (``info.language`` etc.)."""

    __slots__ = ("language", "language_probability", "all_language_probs",
                 "duration", "duration_after_vad")

    def __init__(self, d: dict) -> None:
        self.language: Optional[str] = d.get("language")
        self.language_probability: Optional[float] = d.get("language_probability")
        self.all_language_probs: list = d.get("all_language_probs") or []
        self.duration: Optional[float] = d.get("duration")
        self.duration_after_vad: Optional[float] = d.get("duration_after_vad")


class SttWorkerError(RuntimeError):
    """Structured worker failure (spawn, timeout, closed pipe, decode)."""


# ── daemon-side client ─────────────────────────────────────────────────
class SttWorkerClient:
    """Persistent faster-whisper worker: spawn, JSON-lines request/response.

    ``transcribe`` matches the faster-whisper call shape the listener and
    the dictation engine already use: ``(rows, info)`` with attribute rows
    and ``info.language``. Requests are serialized under one lock; a
    timeout or closed pipe poisons the worker (callers fail closed).
    """

    def __init__(self, cfg: Any, log=None) -> None:
        self._cfg = cfg
        self._log = log or (lambda msg: None)
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._poisoned = False
        self._ready = False
        self.device: str = ""
        self.compute: str = ""
        self.model_name: str = ""
        # Single dedicated stdout reader: one thread owns readline() for the
        # whole lifetime, so a timed-out read can never leave a stale thread
        # racing the next read for the same pipe (that race could swallow a
        # response line and deadlock the request until the timeout).
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self, timeout: float = 300.0) -> bool:
        """Spawn the worker and wait for its model-ready stage."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return self._ready
            try:
                self._spawn_locked()
            except OSError as exc:
                self._log(f"stt-worker spawn failed: {exc}")
                return False
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._poisoned = True
                self._log("stt-worker load timed out")
                return False
            raw = self._read_line_with_timeout(remaining)
            if raw is None:
                self._log("stt-worker exited during model load")
                return False
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            stage = msg.get("stage")
            if stage:
                self._log(f"stt-worker: {stage}")
            if msg.get("ok") is True:
                self._ready = True
                self.device = str(msg.get("device") or "")
                self.compute = str(msg.get("compute") or "")
                self.model_name = str(msg.get("model") or "")
                return True
            if msg.get("ok") is False:
                self._log(f"stt-worker load failed: "
                          f"{msg.get('detail') or msg.get('error') or 'unknown'}")
                return False

    def shutdown(self) -> None:
        """Terminate the worker with its owner; stale responses cannot arrive."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.write(json.dumps({"op": "shutdown"}) + "\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=3.0)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
            except Exception:
                pass

    # ── transcription ────────────────────────────────────────────────
    def transcribe(
        self, audio: Any, language: Optional[str] = None, **kwargs: Any
    ) -> Tuple[List[SegmentRow], InfoRow]:
        b64, length = _audio_to_b64(audio)
        payload = {
            "op": "transcribe",
            "audio_b64": b64,
            "length": length,
            "language": language,
            "kwargs": dict(kwargs or {}),
        }
        resp = self._communicate(payload, timeout=180.0)
        rows = [SegmentRow(d) for d in (resp.get("segments") or [])]
        info = InfoRow(resp.get("info") or {})
        return rows, info

    # ── transport ────────────────────────────────────────────────────
    def _communicate(self, payload: dict, timeout: float = 180.0) -> dict:
        with self._lock:
            if self._poisoned:
                raise SttWorkerError("stt worker poisoned; restart the daemon to recover")
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._poisoned = True
                raise SttWorkerError("stt worker not running")
            self._next_id += 1
            rid = self._next_id
            body = dict(payload)
            body["id"] = rid
            assert proc.stdin is not None and proc.stdout is not None
            try:
                proc.stdin.write(json.dumps(body) + "\n")
                proc.stdin.flush()
            except OSError as exc:
                self._poisoned = True
                raise SttWorkerError(f"stt worker stdin closed: {exc}") from exc
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._poisoned = True
                    raise SttWorkerError(f"stt worker timeout after {timeout}s")
                raw = self._read_line_with_timeout(remaining)
                if raw is None:
                    self._poisoned = True
                    raise SttWorkerError("stt worker closed stdout")
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") != rid:
                    if msg.get("stage"):
                        self._log(f"stt-worker: {msg.get('stage')}")
                    continue
                if not msg.get("ok", False):
                    self._poisoned = True
                    raise SttWorkerError(
                        f"{msg.get('error') or 'decode error'}: "
                        f"{msg.get('detail') or ''}")
                return msg

    def _read_line_with_timeout(self, timeout: float) -> Optional[str]:
        """Next line from the single stdout reader, or ``""`` on timeout.

        A timeout returns the empty string (not EOF) so the caller keeps
        its remaining budget; EOF returns ``None`` once, then repeats.
        """
        try:
            return self._lines.get(timeout=max(0.05, timeout))
        except queue.Empty:
            return ""  # timeout, not EOF: caller keeps the remaining budget

    def _spawn_locked(self) -> None:
        env = dict(os.environ)
        env.update(worker_env_for_cfg(self._cfg))
        env["PYTHONIOENCODING"] = "utf-8"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        if getattr(sys, "frozen", False):
            # Re-exec the frozen bundle; desktop_app.app.main() routes here.
            env[ENV_FROZEN_MARKER] = "1"
            cmd = [sys.executable]
        else:
            # Source run: importable jarvis via src/ on PYTHONPATH.
            src = os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
            cmd = [sys.executable, "-m", "jarvis.listening.fasterwhisper_worker"]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._poisoned = False
        self._ready = False

        # One reader owns stdout.readline() for the whole process lifetime.
        def _read_stdout() -> None:
            try:
                stdout = self._proc.stdout
                if stdout is None:
                    self._lines.put(None)
                    return
                for line in iter(stdout.readline, ""):
                    self._lines.put(line)
            except Exception:
                pass
            finally:
                self._lines.put(None)  # EOF marker

        threading.Thread(target=_read_stdout, name="stt-worker-stdout",
                         daemon=True).start()

        def _drain() -> None:
            try:
                stderr = self._proc.stderr
                if stderr is None:
                    return
                for line in iter(stderr.readline, ""):
                    if line.strip():
                        try:
                            self._log(f"stt-worker: {line.rstrip()}")
                        except Exception:
                            # A log sink failure must never kill the drain
                            # (the pipe would fill and deadlock the worker).
                            pass
            except Exception:
                pass

        threading.Thread(target=_drain, name="stt-worker-stderr",
                         daemon=True).start()


# ── worker process ─────────────────────────────────────────────────────
def _emit(message: dict) -> None:
    try:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()
    except Exception:
        pass


def _say(text: str) -> None:
    """Console output: stderr only, so the JSON stdout protocol stays clean."""
    try:
        print(text, file=sys.stderr, flush=True)
    except Exception:
        pass


def _ensure_stdio() -> None:
    """Frozen windowed builds may start with None std streams; rebind fds."""
    try:
        if sys.stdin is None:
            sys.stdin = open(0, "r", encoding="utf-8", errors="replace", newline="")
    except Exception:
        pass
    try:
        if sys.stdout is None:
            sys.stdout = open(1, "w", encoding="utf-8", errors="replace", newline="")
    except Exception:
        pass


def _load_model(
    model_name: str, device: str, compute: str, cpu_threads: int,
    cache_dir: Optional[str],
) -> Tuple[Any, str, str, dict]:
    """Load WhisperModel with the same fallback ladder as the listener.

    Returns ``(model, used_device, used_compute, resolved_kwargs)`` or
    raises on total failure.
    """
    from faster_whisper import WhisperModel

    if model_name == "large-v3-turbo" and not _is_turbo_supported():
        _say("  ⚠️  large-v3-turbo is not supported by the installed engine, "
             "using large-v3 instead")
        model_name = "large-v3"

    _download_root = cache_dir or None
    if _download_root:
        local_path = _resolve_local(_download_root, model_name)
        if local_path:
            model_name = local_path

    def _kwargs():
        kw = {}
        if _download_root:
            kw["download_root"] = _download_root
        return kw

    resolved_device, missing_libs = _probe_windows_cuda_libraries(device)
    if missing_libs:
        _say(f"  ℹ️  CUDA libraries missing ({', '.join(missing_libs)}), "
             "using CPU mode")
    device = resolved_device

    compute_types = [compute]
    if compute == "int8":
        compute_types.extend(["float16", "float32"])
    elif compute == "float16":
        compute_types.append("float32")
    configs_to_try = []
    for ct in compute_types:
        configs_to_try.append((device, ct))
    if device in ("auto", "cuda"):
        for ct in compute_types:
            configs_to_try.append(("cpu", ct))

    last_error = None
    used_device = device
    used_compute = compute
    for try_device, try_compute in configs_to_try:
        try:
            threads = (os.cpu_count() or 4) if try_device in ("cpu", "auto") else 0
            _say(f"  🎤 Loading Whisper '{model_name}' "
                 f"(device={try_device}, compute={try_compute})...")
            model = WhisperModel(
                model_name, device=try_device, compute_type=try_compute,
                cpu_threads=threads, **_kwargs(),
            )
            used_device = try_device
            used_compute = try_compute
            last_error = None
            break
        except Exception as e:
            last_error = e
            error_str = str(e).lower()
            is_cuda_error = any(x in error_str for x in [
                "cuda", "cublas", "cudnn", "gpu", "nvidia",
                ".dll is not found", "library", "ctypes",
            ])
            is_compute_error = any(x in error_str for x in [
                "compute type", "int8", "float16",
            ])
            if is_cuda_error or is_compute_error:
                _say(f"  config ({try_device}, {try_compute}) failed, "
                     f"trying fallback: {e}")
                continue
            is_corrupted_cache = "unable to open file" in error_str
            if is_corrupted_cache:
                _say("  ⚠️  Whisper model cache appears corrupted, "
                     "attempting recovery...")
                if _clear_corrupted_cache(str(e)):
                    try:
                        threads = ((os.cpu_count() or 4)
                                   if try_device in ("cpu", "auto") else 0)
                        _say(f"  🎤 Re-downloading Whisper '{model_name}'...")
                        model = WhisperModel(
                            model_name, device=try_device,
                            compute_type=try_compute, cpu_threads=threads,
                            **_kwargs(),
                        )
                        used_device = try_device
                        used_compute = try_compute
                        last_error = None
                        break
                    except Exception as retry_e:
                        _say(f"  ❌ retry after cache clear failed: {retry_e}")
                        continue
                else:
                    _say("  ❌ Failed to clear the corrupted cache; "
                         "try deleting the Whisper model cache directory "
                         "and restarting")
                    continue
            is_rate_limited = (
                any(x in error_str for x in ["429", "too many requests", "rate limit"])
                or getattr(getattr(e, "response", None), "status_code", None) == 429
            )
            if is_rate_limited:
                _max_retries = 4
                _backoff = 2
                retry_succeeded = False
                for retry_num in range(1, _max_retries + 1):
                    wait = _backoff ** retry_num
                    _say(f"  ⏳ Rate limited by HuggingFace, retrying in "
                         f"{wait}s ({retry_num}/{_max_retries})...")
                    time.sleep(wait)
                    try:
                        threads = ((os.cpu_count() or 4)
                                   if try_device in ("cpu", "auto") else 0)
                        model = WhisperModel(
                            model_name, device=try_device,
                            compute_type=try_compute, cpu_threads=threads,
                            **_kwargs(),
                        )
                        used_device = try_device
                        used_compute = try_compute
                        last_error = None
                        retry_succeeded = True
                        break
                    except Exception as retry_e:
                        last_error = retry_e
                if retry_succeeded:
                    break
                _say(f"  ❌ Failed to load Whisper model after {_max_retries} "
                     f"retries: {last_error}")
                raise
            _say(f"  ❌ Failed to load Whisper model: {e}")
            raise

    if last_error is not None:
        raise last_error

    resolved_kwargs, rejected = _resolve_transcribe_kwargs(
        getattr(model, "transcribe", None), FASTER_WHISPER_TRANSCRIBE_KWARGS)
    _say(f"  🎤 Whisper '{model_name}' loaded on {used_device} "
         f"(compute={used_compute}); rejected kwargs: {rejected}")
    return model, used_device, used_compute, resolved_kwargs


def worker_main() -> int:
    """The subprocess entry: load once, then serve transcribe requests."""
    _ensure_stdio()
    model_name = str(os.environ.get(ENV_MODEL, "small") or "small")
    cache_dir = (os.environ.get(ENV_CACHE) or "").strip() or None
    device = str(os.environ.get(ENV_DEVICE, "auto") or "auto")
    compute = str(os.environ.get(ENV_COMPUTE, "int8") or "int8")
    language = (os.environ.get(ENV_LANGUAGE) or "").strip() or None
    threads = int(os.environ.get(ENV_THREADS, "0") or 0)

    try:
        model, used_device, used_compute, resolved_kwargs = _load_model(
            model_name, device, compute, threads, cache_dir)
    except Exception as exc:
        _emit({"ok": False, "stage": "load-failed", "op": "load",
               "error": "MODEL_LOAD_FAILED",
               "detail": f"{type(exc).__name__}: {exc}"})
        return 1

    # Warm up so the first real utterance doesn't pay the cold-decode cost.
    # A closed-set selector ("cs+vi") cannot be forced: it warms the
    # auto-detect path, matching the listener's warmup contract.
    warmup_lang = _resolve_forced_language(language)
    try:
        import numpy as np

        rng = np.random.default_rng(0)
        warmup_audio = rng.standard_normal(16000).astype(np.float32) * 0.01
        segments_iter, _ = model.transcribe(
            warmup_audio, language=warmup_lang, **resolved_kwargs)
        for _ in segments_iter:
            pass
        _say("faster-whisper warmup transcription complete")
    except TypeError:
        try:
            segments_iter, _ = model.transcribe(
                warmup_audio, language=warmup_lang)
            for _ in segments_iter:
                pass
            _say("faster-whisper warmup transcription complete")
        except Exception as exc:
            _say(f"faster-whisper warmup failed: {exc}")
    except Exception as exc:
        _say(f"faster-whisper warmup failed: {exc}")

    try:
        import faster_whisper

        version = str(getattr(faster_whisper, "__version__", ""))
    except Exception:
        version = ""
    _emit({"ok": True, "stage": "ready", "op": "load",
           "model": model_name, "device": used_device,
           "compute": used_compute, "version": version})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(req, dict):
            # A primitive or malformed line must never kill the request
            # loop: ignore it and keep serving (the client never sends
            # these, but one bad line must not take STT down).
            continue
        rid = req.get("id")
        try:
            if req.get("op") == "transcribe":
                audio = _audio_from_b64(str(req.get("audio_b64") or ""))
                # A closed-set selector ("cs+vi") is resolved by the caller
                # as per-code forced passes; anything non-single falls back
                # to auto-detect here so no caller can crash the worker.
                lang = _resolve_forced_language(req.get("language") or None)
                merged = dict(resolved_kwargs)
                merged.update(req.get("kwargs") or {})
                merged, _ = _resolve_transcribe_kwargs(model.transcribe, merged)
                segments, info = model.transcribe(audio, language=lang, **merged)
                rows = []
                for seg in segments:
                    rows.append({
                        "text": str(getattr(seg, "text", "") or ""),
                        "avg_logprob": getattr(seg, "avg_logprob", None),
                        "no_speech_prob": getattr(seg, "no_speech_prob", None),
                        "start": float(getattr(seg, "start", 0.0) or 0.0),
                        "end": float(getattr(seg, "end", 0.0) or 0.0),
                        "kv_cache_exhausted": bool(
                            getattr(seg, "kv_cache_exhausted", False) or False),
                    })
                all_probs = getattr(info, "all_language_probs", None)
                info_payload = {
                    "language": getattr(info, "language", None),
                    "language_probability": getattr(
                        info, "language_probability", None),
                    "all_language_probs": (
                        [[str(c), float(p)] for c, p in all_probs]
                        if isinstance(all_probs, (list, tuple)) else []),
                    "duration": getattr(info, "duration", None),
                    "duration_after_vad": getattr(
                        info, "duration_after_vad", None),
                }
                _emit({"id": rid, "ok": True, "segments": rows,
                       "info": info_payload})
            elif req.get("op") == "shutdown":
                _emit({"id": rid, "ok": True})
                return 0
            else:
                _emit({"id": rid, "ok": False, "error": "UNKNOWN_OP",
                       "detail": str(req.get("op"))})
        except Exception as exc:
            _emit({"id": rid, "ok": False,
                   "error": type(exc).__name__, "detail": str(exc)})
    return 0


def main() -> int:
    return worker_main()


if __name__ == "__main__":
    sys.exit(worker_main())