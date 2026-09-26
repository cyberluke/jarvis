"""Runtime discovery and isolated worker launch for the OpenVINO speech backend.

The desktop process never imports the OpenVINO native libraries: discovery is
pure-Python (registry + file checks) and every native import happens inside a
dedicated CPython 3.13 x64 worker process that speaks a bounded, versioned
local stdio JSON protocol (request IDs, explicit audio format/length,
structured responses, logs on stderr). No network server is opened.

Discovery order:

1. An explicit saved runtime-root override (``whisper_openvino_runtime_root``).
   An invalid override is an actionable error, not a licence to pick another
   installation silently.
2. Installer ``InstallDir`` discovery through the Windows registry (both
   registry views; conflicting valid installations are reported).
3. A user-selected installed root passed by the setup UI.

The default ``C:\\Program Files\\Intel`` is only a hint; a root counts as an
installation only when its files validate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: Stable error codes surfaced through Settings/runtime status and logs.
OV_RUNTIME_NOT_FOUND = "OV_RUNTIME_NOT_FOUND"
OV_RUNTIME_ABI_MISMATCH = "OV_RUNTIME_ABI_MISMATCH"
OV_COMPANION_MISSING = "OV_COMPANION_MISSING"
OV_RUNTIME_VERSION_MISMATCH = "OV_RUNTIME_VERSION_MISMATCH"
OV_NPU_UNAVAILABLE = "OV_NPU_UNAVAILABLE"
OV_MODEL_INCOMPLETE = "OV_MODEL_INCOMPLETE"
OV_MODEL_COMPILE_FAILED = "OV_MODEL_COMPILE_FAILED"
OV_DECODE_POLICY_UNSUPPORTED = "OV_DECODE_POLICY_UNSUPPORTED"
OV_WHISPER_METRICS_UNAVAILABLE = "OV_WHISPER_METRICS_UNAVAILABLE"
OV_WHISPER_PAIR_UNSUPPORTED = "OV_WHISPER_PAIR_UNSUPPORTED"

#: Expected identities of the owner-supplied custom build. The loaded values
#: are compared against these at startup; the comparison is reported, never
#: assumed. Production lane: the custom 2026.4 cp314 build installed under
#: ``C:\Python314`` (source worktree commit ``f62ebcf0``).
EXPECTED_CORE_BUILD = "2026.4.0-22959"
EXPECTED_CORE_WHEEL = "openvino-2026.4.0-22959-cp314-cp314-win_amd64.whl"
EXPECTED_GENAI_WHEEL = "openvino_genai-2026.4.0.0-cp314-cp314-win_amd64.whl"
#: GenAI source identity recorded from the installed companion (the version
#: string of the built module; the Core build number alone does not
#: establish it). The production NPU-lane source worktree HEAD is ``f62ebcf0``.
GENAI_SOURCE_COMMIT = "f62ebcf0"

#: Score-semantics contract of the patched companion (same value as
#: ``openvino_models.SCORE_SEMANTICS``; repeated here to keep this module
#: dependency-light for the worker side).
SCORE_SEMANTICS = "ov-genai-whisper-logprob-v1"

#: The production interpreter for the validated NPU lane. It carries the
#: custom 2026.4.0-22959 Core and the patched GenAI companion.
PRODUCTION_PYTHON = r"C:\Python314\python.exe"

#: Hint only; validated file presence is what proves an installation.
DEFAULT_INSTALL_ROOT = r"C:\Program Files\Intel"

_REG_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Intel"

_WORKER_FILE = Path(__file__).with_name("openvino_worker.py")


class OVWhisperError(RuntimeError):
    """Structured backend failure with a stable error code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _validate_root(root: str) -> Optional[Dict[str, Any]]:
    """Validate one candidate install root; ``None`` when it is not one."""
    try:
        base = Path(str(root)).expanduser()
        if not base.is_dir():
            return None
        release = base / "runtime" / "bin" / "intel64" / "Release"
        if not (release / "openvino.dll").is_file():
            return None
        py_dir = base / "python"
        if not py_dir.is_dir():
            return None
        pyds = sorted(py_dir.glob("openvino/_pyopenvino*.pyd")) + sorted(py_dir.glob("_pyopenvino*.pyd"))
        if not pyds:
            return None
        tbb = base / "runtime" / "3rdparty" / "tbb" / "bin"
        return {
            "root": str(base),
            "lib_dirs": [str(release)] + ([str(tbb)] if tbb.is_dir() else []),
            "python_dir": str(py_dir),
            "pyd": str(pyds[0]),
        }
    except OSError:
        return None


def _registry_install_dirs() -> List[str]:
    """``InstallDir`` values from both registry views, de-duplicated."""
    found: List[str] = []
    try:
        import winreg

        for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _REG_KEY, 0, winreg.KEY_READ | flag) as key:
                    value, _type = winreg.QueryValueEx(key, "InstallDir")
                    text = str(value or "").strip()
                    if text and text not in found:
                        found.append(text)
            except OSError:
                continue
    except Exception:
        pass
    return found


def _find_genai_package(python_dir: str, root: str) -> str:
    """Locate the ``openvino_genai`` package directory for the selected Core."""
    candidates = []
    if python_dir:
        candidates.append(str(python_dir))
    if root:
        lab = Path(root).expanduser().parent
        candidates.append(str(lab / "openvino.genai" / ".py-build-cmake_cache" / "cp313-cp313-win_amd64"))
        candidates.append(str(lab / "openvino.genai"))
    for base in candidates:
        try:
            if base and os.path.isfile(os.path.join(base, "openvino_genai", "__init__.py")):
                return base
        except OSError:
            continue
    return ""


def _probe_python_runtime(python: str) -> Optional[Dict[str, Any]]:
    """Validate one interpreter as a complete OpenVINO runtime, or ``None``."""
    snippet = (
        "import json,sys;"
        "from importlib.metadata import version;"
        "import openvino,openvino_genai;"
        "from openvino import Core;"
        "print(json.dumps({"
        "'python':sys.executable,"
        "'python_version':'.'.join(str(p) for p in sys.version_info[:3]),"
        "'abi':'cp%d%d'%(sys.version_info[0],sys.version_info[1]),"
        "'openvino':version('openvino'),"
        "'openvino_genai':version('openvino-genai'),"
        "'openvino_tokenizers':version('openvino-tokenizers'),"
        "'genai_file':openvino_genai.__file__,"
        "'devices':[str(d) for d in Core().available_devices]}))"
    )
    try:
        result = subprocess.run(
            [python, "-c", snippet],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("devices"):
        return None
    return data


def discover_runtime(cfg: Any) -> Dict[str, Any]:
    """Resolve the selected runtime source into a validated installation.

    Returns ``{"source", "root", "lib_dirs", "python_dir", "pyd", "code",
    "detail", "conflicts"}``. ``code`` is empty on success. For the
    ``python`` source the payload additionally carries ``probe`` with the
    live interpreter/package identities.
    """
    source = str(getattr(cfg, "whisper_openvino_runtime_source", "installed") or "installed").lower()
    override = str(getattr(cfg, "whisper_openvino_runtime_root", "") or "").strip()

    if source == "wheel":
        wheels = _find_wheels()
        if wheels is None:
            return {"source": source, "code": OV_RUNTIME_NOT_FOUND,
                    "detail": f"expected {EXPECTED_CORE_WHEEL} (and the matching GenAI wheel) not found",
                    "conflicts": []}
        return {"source": source, "code": "", "detail": "", "wheels": wheels, "conflicts": []}

    if source == "python":
        # One self-contained interpreter: the packages live in its own
        # site-packages, so no PYTHONPATH/DLL wiring is needed.
        explicit = str(getattr(cfg, "whisper_openvino_python", "") or "").strip()
        for candidate in (explicit, PRODUCTION_PYTHON):
            if not candidate or not os.path.isfile(candidate):
                continue
            probe = _probe_python_runtime(candidate)
            if probe is None:
                return {"source": source, "code": OV_COMPANION_MISSING,
                        "detail": f"interpreter '{candidate}' lacks openvino/openvino_genai",
                        "conflicts": []}
            return {"source": source, "code": "", "detail": "", "conflicts": [],
                    "python": candidate, "probe": probe}
        return {"source": source, "code": OV_RUNTIME_NOT_FOUND,
                "detail": "no configured production interpreter found", "conflicts": []}

    # Installed-tree route.
    if override:
        info = _validate_root(override)
        if info is None:
            return {"source": source, "code": OV_RUNTIME_NOT_FOUND,
                    "detail": f"explicit runtime root '{override}' is not a valid installation",
                    "conflicts": []}
        info["genai_dir"] = _find_genai_package(info.get("python_dir", ""), info["root"])
        return {**info, "source": source, "code": "", "detail": "", "conflicts": []}

    candidates = _registry_install_dirs()
    if not candidates:
        candidates = [DEFAULT_INSTALL_ROOT]
    valid: List[Dict[str, Any]] = []
    for cand in candidates:
        info = _validate_root(cand)
        if info is not None and info not in valid:
            valid.append(info)
    if not valid:
        return {"source": source, "code": OV_RUNTIME_NOT_FOUND,
                "detail": "no validated installation under registry InstallDir or the default hint",
                "conflicts": []}
    if len(valid) > 1:
        return {"source": source, "code": OV_RUNTIME_NOT_FOUND,
                "detail": "conflicting valid installations: " + " | ".join(v["root"] for v in valid),
                "conflicts": [v["root"] for v in valid]}
    chosen = valid[0]
    chosen["genai_dir"] = _find_genai_package(chosen.get("python_dir", ""), chosen["root"])
    return {**chosen, "source": source, "code": "", "detail": "", "conflicts": []}


def _find_wheels() -> Optional[Dict[str, str]]:
    """Locate the exact owner-supplied wheels for the wheel route."""
    search_roots = [DEFAULT_INSTALL_ROOT]
    lab = Path(r"D:\_SATIN_AI_2\openvino-master-lab")
    if lab.is_dir():
        search_roots += [str(lab / "build-ov" / "wheels"), str(lab / "openvino.genai" / "dist")]
    try:
        search_roots += [str(Path(sys.executable).parent)]
    except Exception:
        pass
    found: Dict[str, str] = {}
    for root in search_roots:
        try:
            if not root or not os.path.isdir(root):
                continue
            for name in (EXPECTED_CORE_WHEEL, EXPECTED_GENAI_WHEEL):
                candidate = os.path.join(root, name)
                if os.path.isfile(candidate):
                    found.setdefault(name, candidate)
        except OSError:
            continue
    if EXPECTED_CORE_WHEEL in found and EXPECTED_GENAI_WHEEL in found:
        return found
    return None


def resolve_worker_python(cfg: Any) -> Tuple[Optional[str], str]:
    """Pick the interpreter for the worker.

    Production order: an explicit saved interpreter; the validated 2026.4
    cp314 NPU-lane interpreter (``C:\\Python314\\python.exe``); the frozen
    desktop interpreter when it already carries the full OpenVINO pair; the
    managed compatible workers (``py -3.14`` / ``py -3.13``, then
    ``python3.x``). Returns ``(path, code)``.
    """
    explicit = str(getattr(cfg, "whisper_openvino_python", "") or "").strip()
    if explicit:
        if os.path.isfile(explicit):
            return explicit, ""
        return None, OV_RUNTIME_NOT_FOUND

    if os.path.isfile(PRODUCTION_PYTHON):
        return PRODUCTION_PYTHON, ""

    for candidate in (sys.executable,):
        if _interpreter_has_genai(candidate):
            return candidate, ""

    for minor in ("3.14", "3.13"):
        try:
            result = subprocess.run(
                ["py", f"-{minor}", "-c", "import sys,struct;print(sys.executable);print(struct.calcsize('P')*8)"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            if result.returncode == 0:
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                if len(lines) >= 2 and lines[1] == "64" and os.path.isfile(lines[0]):
                    return lines[0], ""
        except Exception:
            pass
        for name in (f"python{minor}", f"python{minor}.exe"):
            path = _which(name)
            if path and _interpreter_is_x64(path):
                return path, ""
    return None, OV_RUNTIME_ABI_MISMATCH


def _which(name: str) -> Optional[str]:
    import shutil

    return shutil.which(name)


def _interpreter_has_genai(exe: str) -> bool:
    try:
        if not exe or not os.path.isfile(exe):
            return False
        result = subprocess.run(
            [exe, "-c", "import openvino, openvino_genai"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        return result.returncode == 0
    except Exception:
        return False


def _interpreter_is_x64(exe: str) -> bool:
    try:
        if not exe or not os.path.isfile(exe):
            return False
        result = subprocess.run(
            [exe, "-c", "import struct;print(struct.calcsize('P')*8)"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if result.returncode != 0:
            return False
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return bool(lines) and lines[0] == "64"
    except Exception:
        return False


def build_worker_env(discovery: Dict[str, Any]) -> Dict[str, str]:
    """Child environment for one selected runtime source.

    Only the worker receives these variables: running setupvars in another
    shell does not mutate the parent, so the equivalent child environment is
    constructed here. Windows paths are quoted by ``subprocess`` itself.
    """
    env = dict(os.environ)
    source = str(discovery.get("source") or "installed")
    if source == "python":
        # The selected interpreter resolves everything from its own
        # site-packages; no extra wiring.
        return env
    if source == "wheel":
        wheels = discovery.get("wheels") or {}
        # The dedicated environment is created from the exact wheels; the
        # worker only needs the package directories that ``pip`` unpacked.
        lib_dirs: List[str] = []
        for name, path in wheels.items():
            site = os.path.join(os.path.dirname(path), "site-" + name.split("-")[0])
            if os.path.isdir(site):
                lib_dirs.append(site)
        if lib_dirs:
            env["PYTHONPATH"] = os.pathsep.join(lib_dirs)
            env["OPENVINO_LIB_PATHS"] = os.pathsep.join(lib_dirs)
            env["PATH"] = os.pathsep.join(lib_dirs) + os.pathsep + env.get("PATH", "")
        return env

    lib_dirs = list(discovery.get("lib_dirs") or [])
    python_dir = str(discovery.get("python_dir") or "")
    genai_dir = str(discovery.get("genai_dir") or "")
    if lib_dirs:
        env["OPENVINO_LIB_PATHS"] = os.pathsep.join(lib_dirs)
        env["PATH"] = os.pathsep.join(lib_dirs) + os.pathsep + env.get("PATH", "")
    python_paths = [p for p in (python_dir, genai_dir) if p]
    if python_paths:
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(python_paths) + (os.pathsep + existing if existing else "")
    return env


class OVSpeechWorker:
    """Bounded stdio JSON worker for one selected runtime source.

    One persistent loaded pipeline per worker. Requests carry IDs; responses
    are matched by ID; stderr is drained to the debug log so the pipes cannot
    deadlock. Failures poison the worker: the owner recreates it explicitly,
    repeated import/compile failures do not loop.
    """

    def __init__(self, cfg: Any, log: Optional[Any] = None) -> None:
        self._log = log or _default_log
        self._discovery = discover_runtime(cfg)
        if self._discovery.get("code"):
            raise OVWhisperError(self._discovery["code"], self._discovery.get("detail", ""))
        python, code = resolve_worker_python(cfg)
        if python is None:
            raise OVWhisperError(code, "no CPython 3.13 x64 interpreter found")
        self.python = python
        self._env = build_worker_env(self._discovery)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                [python, "-u", str(_WORKER_FILE)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._env,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise OVWhisperError(OV_RUNTIME_NOT_FOUND, f"worker launch failed: {exc}") from exc
        self._next_id = 0
        self._poisoned = False
        self._stderr_done = threading.Event()

        def _drain() -> None:
            try:
                stderr = self._proc.stderr
                if stderr is None:
                    return
                for line in iter(stderr.readline, ""):
                    if line.strip():
                        self._log(f"ov-worker: {line.rstrip()}")
            except Exception:
                pass
            finally:
                self._stderr_done.set()

        self._stderr_thread = threading.Thread(target=_drain, name="ov-worker-stderr", daemon=True)
        self._stderr_thread.start()

    @property
    def discovery(self) -> Dict[str, Any]:
        return self._discovery

    def request(self, payload: Dict[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
        """Send one request, return the matching response (by ID).

        Stage progress lines (no ``id`` collision) are forwarded to the log.
        A timeout or a closed pipe poisons the worker.
        """
        if self._poisoned:
            raise OVWhisperError(OV_RUNTIME_NOT_FOUND, "worker poisoned; restart the daemon to recover")
        self._next_id += 1
        request_id = self._next_id
        body = dict(payload)
        body["id"] = request_id
        assert self._proc.stdin is not None and self._proc.stdout is not None
        try:
            self._proc.stdin.write(json.dumps(body) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            self._poisoned = True
            raise OVWhisperError(OV_RUNTIME_NOT_FOUND, f"worker stdin closed: {exc}") from exc

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._poisoned = True
                raise OVWhisperError(OV_RUNTIME_NOT_FOUND, f"worker timeout after {timeout}s")
            line = self._read_line_with_timeout(remaining)
            if line is None:
                self._poisoned = True
                raise OVWhisperError(OV_RUNTIME_NOT_FOUND, "worker closed stdout")
            try:
                message = json.loads(line)
            except ValueError:
                self._log(f"ov-worker: non-JSON line: {line[:200]}")
                continue
            if "stage" in message and "ok" not in message:
                self._log(f"ov-worker: [{message.get('op', '?')}] {message['stage']}")
                continue
            if "phase_ms" in message and "ok" not in message:
                self._log(f"ov-worker: phase {message.get('phase', '?')} = {message.get('phase_ms', '?')} ms")
                continue
            if message.get("id") == request_id:
                if not message.get("ok", False):
                    code = str(message.get("error") or OV_RUNTIME_NOT_FOUND)
                    raise OVWhisperError(code, str(message.get("detail", "")))
                return message
            if "stage" in message:
                self._log(f"ov-worker: [{message.get('op', '?')}] {message['stage']}")

    def _read_line_with_timeout(self, timeout: float) -> Optional[str]:
        result: List[Optional[str]] = [None]

        def _read() -> None:
            try:
                stdout = self._proc.stdout
                if stdout is not None:
                    result[0] = stdout.readline()
            except Exception:
                result[0] = None

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        reader.join(timeout=max(0.05, timeout))
        if reader.is_alive():
            return ""  # timeout, not EOF: caller keeps the remaining budget
        return result[0] or None

    def close(self) -> None:
        """Terminate the worker with its owner; stale responses cannot arrive."""
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.write(json.dumps({"id": 0, "op": "shutdown"}) + "\n")
                self._proc.stdin.flush()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        self._stderr_done.wait(timeout=1.0)

    def validate_identity(self, handshake: Dict[str, Any], device: str,
                          pipeline_mode: str) -> None:
        """Reject a worker whose reported identity is not the production lane.

        The handshake is the authoritative child identity: interpreter,
        companion location, device presence, pipeline mode and score
        semantics are checked here, before any native decode. A mismatch is
        an explicit failure; no alternate lane is selected.
        """
        def _fail(code: str, detail: str) -> None:
            self._poisoned = True
            raise OVWhisperError(code, detail)

        expected_python = str(self._discovery.get("python") or self.python or "")
        reported_python = str(handshake.get("python_executable") or "")
        if expected_python and reported_python:
            if os.path.normcase(os.path.abspath(reported_python)) != \
                    os.path.normcase(os.path.abspath(expected_python)):
                _fail(OV_RUNTIME_VERSION_MISMATCH,
                      f"worker interpreter '{reported_python}' != selected '{expected_python}'")
        genai_file = str(handshake.get("genai_file") or "")
        if not genai_file:
            _fail(OV_COMPANION_MISSING, "worker reports no openvino_genai module file")
        npu = handshake.get("npu")
        if device.upper().startswith("NPU") and not npu:
            _fail(OV_NPU_UNAVAILABLE,
                  f"NPU absent from worker devices: {handshake.get('devices')}")
        if str(handshake.get("score_semantics") or "") != SCORE_SEMANTICS:
            _fail(OV_WHISPER_METRICS_UNAVAILABLE,
                  "worker companion lacks the ov-genai-whisper-logprob-v1 fields")
        if pipeline_mode not in ("stateful", "static"):
            _fail(OV_DECODE_POLICY_UNSUPPORTED, f"unknown pipeline mode '{pipeline_mode}'")


def _default_log(text: str) -> None:
    try:
        from ..debug import debug_log

        debug_log(text, "voice")
    except Exception:
        pass
