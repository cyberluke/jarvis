"""Isolated CPython 3.13 x64 worker for the OpenVINO speech backend.

Runs under the interpreter selected by ``openvino_runtime``. All OpenVINO
native imports happen here; the desktop process never loads these DLLs. The
protocol is a bounded, versioned local stdio JSON contract: one object per
line, request IDs, explicit audio format/length, structured responses, logs
on stderr. No network server is opened.

Protocol (version 1) operations:

- ``handshake``: report Python ABI, Core file/version, GenAI version/commit,
  available devices, NPU presence, and the score-semantics capability.
- ``load``: compile the selected IR pipeline on the explicit device with
  stage progress lines and timing.
- ``transcribe``: one forced-language (or auto) decode of the given float
  PCM16k samples; returns per-window rows with genuine ``avg_logprob`` and
  ``no_speech_prob`` when the companion exposes them.
- ``status``: current initialization phase and identities.
- ``shutdown``: exit.
"""

from __future__ import annotations

import json
import os
import sys
import time

PROTOCOL_VERSION = 1
SCORE_SEMANTICS = "ov-genai-whisper-logprob-v1"
GENAI_SOURCE_COMMIT = "f62ebcf0"
EXPECTED_CORE_BUILD = "2026.4.0-22959"
PRODUCTION_PYTHON = r"C:\Python314\python.exe"
DEFAULT_CACHE_ROOT = r"D:\_SATIN_AI\Toastovac\jarvis\.cache\openvino-npu"


def _log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _phase(name: str, started: float) -> float:
    """Emit one structured startup phase line; return elapsed ms."""
    elapsed = (time.monotonic() - started) * 1000.0
    _emit({"phase_ms": round(elapsed, 3), "phase": name})
    return elapsed


def _add_native_dirs() -> list:
    """Register DLL directories before any native import; keep the handles."""
    handles = []
    raw = os.environ.get("OPENVINO_LIB_PATHS", "")
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part and os.path.isdir(part):
            try:
                handles.append(os.add_dll_directory(part))
            except (OSError, AttributeError):
                pass
    return handles


class _State:
    def __init__(self) -> None:
        self.phase = "init"
        self.pipeline = None
        self.model_dir = ""
        self.precision = ""
        self.device = ""
        self.pipeline_mode = "stateful"
        self.no_speech_token_id = None
        self.load_ms = 0.0
        self.first_compile = True


def _emit(message: dict) -> None:
    print(json.dumps(message), flush=True)


def _stage(request_id: int, op: str, stage: str) -> None:
    _emit({"id": request_id, "op": op, "stage": stage})


def _handle_handshake(state: _State, ov, genai) -> dict:
    devices = []
    npu = None
    try:
        t0 = time.monotonic()
        core = ov.Core()
        _phase("openvino_core_init_ms", t0)
        t1 = time.monotonic()
        devices = [str(d) for d in core.available_devices]
        npu = next((d for d in devices if "NPU" in d.upper()), None)
        _phase("device_discovery_ms", t1)
    except Exception as exc:  # pragma: no cover - reported as a code below
        _log(f"core probe failed: {exc}")
    version = ""
    try:
        version = str(ov.get_version())
    except Exception:
        pass
    genai_version = str(getattr(genai, "__version__", ""))
    genai_file = str(getattr(genai, "__file__", ""))
    dist_versions = {}
    for dist in ("openvino", "openvino-genai", "openvino-tokenizers"):
        try:
            from importlib.metadata import version as _dist_version

            dist_versions[dist] = _dist_version(dist)
        except Exception:
            dist_versions[dist] = ""
    semantics = SCORE_SEMANTICS
    # Capability probe on the native class: the top-level package may expose
    # a legacy Python wrapper, while ``generate`` returns the pybind class
    # from ``py_openvino_genai``. The patched native class carries the
    # genuine fields.
    native = getattr(genai, "py_openvino_genai", genai)
    probe = (
        getattr(native, "WhisperDecodedResults", None)
        or getattr(genai, "WhisperDecodedResults", None)
        or getattr(genai, "ASRDecodedResults", None)
    )
    fields = {name for name in dir(probe) if not name.startswith("_")} if probe is not None else set()
    if "avg_logprobs" not in fields or "no_speech_probs" not in fields:
        semantics = "legacy-constant-1f"
    return {
        "ok": True,
        "proto": PROTOCOL_VERSION,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "python_executable": str(sys.executable),
        "abi": f"cp{sys.version_info[0]}{sys.version_info[1]}",
        "core_file": str(getattr(ov, "__file__", "")),
        "core_version": version,
        "core_expected": EXPECTED_CORE_BUILD,
        "genai_version": genai_version,
        "genai_file": genai_file,
        "genai_commit": GENAI_SOURCE_COMMIT,
        "dist_versions": dist_versions,
        "devices": devices,
        "npu": npu,
        "score_semantics": semantics,
        "pair_capability": semantics == SCORE_SEMANTICS,
        "phase": state.phase,
    }


def _cache_dir_for(model_dir: str, device: str, pipeline_mode: str) -> str:
    """Deterministic per-identity cache root.

    One subfolder per (model artifact identity, device, pipeline mode); the
    NPU plugin keeps the compiled blobs inside. The OpenVINO core build is
    part of the identity because the plugin embeds it in the cache index and
    invalidates mismatches itself. Never shared across incompatible lanes;
    a miss simply recompiles on the same device.
    """
    model_identity = os.path.basename(str(model_dir).rstrip("\\/")) or "model"
    core_build = "unknown"
    try:
        import openvino as _ov

        core_build = str(_ov.get_version())
    except Exception:
        pass
    identity = f"{model_identity}-{device}-{pipeline_mode}-{core_build}"
    identity = identity.replace(":", "").replace("/", "_").replace("\\", "_")
    return os.path.join(DEFAULT_CACHE_ROOT, identity)


def _handle_load(state: _State, genai, message: dict) -> dict:
    model_dir = str(message.get("model_dir", ""))
    device = str(message.get("device", "NPU"))
    state.precision = str(message.get("precision", "int8"))
    state.no_speech_token_id = message.get("no_speech_token_id")
    pipeline_mode = str(message.get("pipeline", "stateful") or "stateful").lower()
    request_id = int(message.get("id", 0))
    started = time.monotonic()
    _stage(request_id, "load", "compiling")
    state.phase = "compiling"
    cache_dir = str(message.get("cache_dir") or "") or _cache_dir_for(model_dir, device, pipeline_mode)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        cache_dir = ""
    props: dict = {}
    if cache_dir:
        props["CACHE_DIR"] = cache_dir
    if pipeline_mode == "static":
        # Explicit diagnostic mode; never an automatic fallback.
        props["STATIC_PIPELINE"] = True
    t0 = time.monotonic()
    # The pybind constructor takes keyword properties, not a mapping.
    state.pipeline = genai.WhisperPipeline(model_dir, device, **props)
    _phase("pipeline_construct_ms", t0)
    state.pipeline_mode = pipeline_mode if pipeline_mode in ("stateful", "static") else "stateful"
    state.model_dir = model_dir
    state.device = device
    state.load_ms = (time.monotonic() - started) * 1000.0
    state.phase = "ready"
    return {
        "ok": True,
        "load_ms": round(state.load_ms, 3),
        "first_compile": state.first_compile,
        "device": device,
        "pipeline": state.pipeline_mode,
        "cache_dir": cache_dir,
        "phase": state.phase,
    }


def _decode_rows(pipeline, genai, audio, language, task, no_speech_token_id, audio_len_samples):
    config = pipeline.get_generation_config()
    config.return_timestamps = False
    config.task = task
    if language:
        config.language = language
    # Greedy-only production contract: the Whisper beam path is broken
    # upstream in the tested 2026.4/2026.5 family (the unpatched PyPI wheel
    # raises "vector too long"; the patched build AVs). Enforce before the
    # native call; the effective value is echoed in the response.
    config.num_beams = 1
    started = time.monotonic()
    result = pipeline.generate(audio, config)
    latency_ms = (time.monotonic() - started) * 1000.0

    texts = list(result.texts)
    scores = list(result.scores)
    languages = list(getattr(result, "languages", []) or [])
    if not languages and getattr(result, "language", ""):
        languages = [result.language]

    avg_logprobs = list(getattr(result, "avg_logprobs", []) or [])
    no_speech_probs = list(getattr(result, "no_speech_probs", []) or [])
    sum_logprobs = list(getattr(result, "sum_logprobs", []) or [])
    token_counts = list(getattr(result, "token_counts", []) or [])
    finish_reasons = list(getattr(result, "finish_reasons", []) or [])
    kv_exhausted = list(getattr(result, "kv_cache_exhausted", []) or [])
    semantics = str(getattr(result, "score_semantics", "") or "")

    chunks = getattr(result, "chunks", None)
    rows = []
    if chunks is not None and len(chunks) > 0:
        window = chunks[0] if isinstance(chunks[0], list) else chunks
        for index, chunk in enumerate(window):
            rows.append({
                "text": str(chunk.text),
                "start_time": float(chunk.start_ts),
                "end_time": float(chunk.end_ts),
                "avg_logprob": _at(avg_logprobs, index),
                "no_speech_prob": _at(no_speech_probs, index),
                "sum_logprob": _at(sum_logprobs, index),
                "token_count": _at(token_counts, index),
                "finish_reason": _finish_token(_at(finish_reasons, index)),
                "kv_cache_exhausted": bool(_at(kv_exhausted, index) or False),
                "score": _at(scores, index),
            })
    else:
        # No-timestamp path: one short-utterance window labelled with the
        # source-audio interval.
        rows.append({
            "text": texts[0] if texts else "",
            "start_time": 0.0,
            "end_time": round(audio_len_samples / 16000.0, 4),
            "avg_logprob": _at(avg_logprobs, 0),
            "no_speech_prob": _at(no_speech_probs, 0),
            "sum_logprob": _at(sum_logprobs, 0),
            "token_count": _at(token_counts, 0),
            "finish_reason": _finish_token(_at(finish_reasons, 0)),
            "kv_cache_exhausted": bool(_at(kv_exhausted, 0) or False),
            "score": _at(scores, 0),
        })
    return rows, (languages[0] if languages else (language or "")), semantics, latency_ms


def _finish_token(value):
    """Plain finish-reason token for the JSON protocol."""
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(value).rsplit(".", 1)[-1]


def _at(values, index):
    try:
        return values[index]
    except (IndexError, TypeError):
        return None


_IMPORT_T0 = time.perf_counter()


def main() -> int:
    _add_native_dirs()
    try:
        t0 = time.perf_counter()
        import openvino as ov
        import openvino_genai as genai
        _phase("python_import_ms", _IMPORT_T0)
    except Exception as exc:
        _emit({"id": 0, "ok": False, "error": "OV_COMPANION_MISSING", "detail": str(exc)})
        return 1

    state = _State()
    stdin = sys.stdin
    while True:
        line = stdin.readline()
        if not line:
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as exc:
            _emit({"id": 0, "ok": False, "error": "OV_DECODE_POLICY_UNSUPPORTED", "detail": f"bad json: {exc}"})
            continue
        request_id = int(message.get("id", 0))
        op = str(message.get("op", ""))
        try:
            if op == "shutdown":
                return 0
            if op == "handshake":
                _emit({**_handle_handshake(state, ov, genai), "id": request_id})
            elif op == "load":
                _emit({**_handle_load(state, genai, message), "id": request_id})
            elif op == "transcribe":
                if state.pipeline is None:
                    _emit({"id": request_id, "ok": False, "error": "OV_MODEL_INCOMPLETE",
                           "detail": "pipeline not loaded"})
                    continue
                audio = message.get("audio") or []
                length = int(message.get("length", len(audio)))
                if len(audio) != length:
                    _emit({"id": request_id, "ok": False, "error": "OV_MODEL_INCOMPLETE",
                           "detail": f"audio length mismatch: {len(audio)} != {length}"})
                    continue
                # Beam guard before any native call: the Whisper beam path is
                # broken upstream in the tested 2026.4/2026.5 family.
                requested_beams = message.get("num_beams")
                if requested_beams is not None and int(requested_beams) != 1:
                    _emit({"id": request_id, "ok": False, "error": "OV_DECODE_POLICY_UNSUPPORTED",
                           "detail": f"num_beams={int(requested_beams)} rejected: greedy-only "
                                     "(upstream beam defect in 2026.4/2026.5)"})
                    continue
                rows, language, semantics, latency_ms = _decode_rows(
                    state.pipeline, genai, [float(sample) for sample in audio],
                    message.get("language"), str(message.get("task", "transcribe")),
                    state.no_speech_token_id, length,
                )
                if not semantics:
                    semantics = "legacy-constant-1f"
                _emit({"id": request_id, "ok": True, "rows": rows, "language": language,
                       "score_semantics": semantics, "latency_ms": round(latency_ms, 3),
                       "device": state.device, "pipeline": state.pipeline_mode,
                       "num_beams": 1,
                       "audio_format": "f32le-mono-16k", "audio_length": length})
            elif op == "status":
                _emit({"id": request_id, "ok": True, "phase": state.phase,
                       "model_dir": state.model_dir, "precision": state.precision,
                       "device": state.device, "pipeline": state.pipeline_mode,
                       "num_beams": 1,
                       "load_ms": round(state.load_ms, 3),
                       "first_compile": state.first_compile})
            else:
                _emit({"id": request_id, "ok": False, "error": "OV_DECODE_POLICY_UNSUPPORTED",
                       "detail": f"unknown op '{op}'"})
        except Exception as exc:
            detail = str(exc)
            code = "OV_MODEL_COMPILE_FAILED" if op == "load" else "OV_MODEL_INCOMPLETE"
            if "NPU" in detail.upper() and "not found" in detail.lower():
                code = "OV_NPU_UNAVAILABLE"
            _emit({"id": request_id, "ok": False, "error": code, "detail": detail})


def _diagnose_main(argv: list) -> int:
    """Diagnostic mode over the same worker/adapter used in production.

    Contract (implemented here, printed by --help):
      --diagnose --runtime-manifest R.json --model-manifest M.json
      --device NPU|CPU --language cs+vi|cs|vi --audio A.wav --report R.json

    The runtime manifest names one installation (root/lib dirs/python dir/
    genai dir + expected Core build). The model manifest names the artifact
    (model, precision, repo id, revision, dir, no_speech_token_id). All
    identity, placement, statistics, pair-selection and timing evidence goes
    to the report; a ``resolved-run-inputs.json`` with the concrete paths,
    identities, audio hash and effective decode settings is written next to it.
    """
    import argparse
    import hashlib

    ap = argparse.ArgumentParser(prog="openvino_worker --diagnose")
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--runtime-manifest", required=True)
    ap.add_argument("--model-manifest", required=True)
    ap.add_argument("--device", default="NPU")
    ap.add_argument("--language", default="cs+vi")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--report", required=True)
    args = ap.parse_args(argv)

    def sha(path: str) -> str:
        try:
            h = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return ""

    report: dict = {"schema_version": PROTOCOL_VERSION, "diagnostic": True}
    with open(args.runtime_manifest, "r", encoding="utf-8") as handle:
        runtime = json.load(handle)
    with open(args.model_manifest, "r", encoding="utf-8") as handle:
        model = json.load(handle)

    # --- DLL/PATH registration for exactly one runtime profile -------------
    for part in (runtime.get("lib_dirs") or []):
        if part and os.path.isdir(part):
            try:
                os.add_dll_directory(part)
            except (OSError, AttributeError):
                pass
    for part in (runtime.get("lib_dirs") or []):
        if part:
            os.environ["PATH"] = part + os.pathsep + os.environ.get("PATH", "")
    for key in ("python_dir", "genai_dir"):
        value = runtime.get(key) or ""
        if value and os.path.isdir(value) and value not in sys.path:
            sys.path.insert(0, value)

    try:
        import openvino as ov
        import openvino_genai as genai
    except Exception as exc:
        report["error"] = {"code": "OV_COMPANION_MISSING", "detail": str(exc)}
        _write_report(args.report, report)
        return 1

    core = ov.Core()
    devices = [str(d) for d in core.available_devices]
    npu = next((d for d in devices if "NPU" in d.upper()), None)
    identity = {
        "interpreter": sys.executable,
        "python": ".".join(str(p) for p in sys.version_info[:3]),
        "abi": f"cp{sys.version_info[0]}{sys.version_info[1]}",
        "core_file": str(getattr(ov, "__file__", "")),
        "core_version": str(ov.get_version()),
        "core_expected": runtime.get("expected_core_build", ""),
        "genai_file": str(getattr(genai, "__file__", "")),
        "genai_version": str(getattr(genai, "__version__", "")),
        "devices": devices,
        "npu": npu,
        "dll_hashes": {},
    }
    for part in (runtime.get("lib_dirs") or []):
        candidate = os.path.join(part, "openvino.dll")
        if os.path.isfile(candidate):
            identity["dll_hashes"]["openvino.dll"] = sha(candidate)
    pyd = runtime.get("pyd") or ""
    if pyd and os.path.isfile(pyd):
        identity["dll_hashes"]["_pyopenvino"] = sha(pyd)
    genai_dir = runtime.get("genai_dir") or ""
    if genai_dir:
        for name in ("openvino_genai.dll",):
            candidate = os.path.join(genai_dir, "openvino_genai", name)
            if not os.path.isfile(candidate):
                candidate = os.path.join(genai_dir, name)
            if os.path.isfile(candidate):
                identity["dll_hashes"][name] = sha(candidate)
    report["identity"] = identity

    # --- audio identity -----------------------------------------------------
    import numpy as np

    with open(args.audio, "rb") as handle:
        audio_bytes = handle.read()
    audio_sha = hashlib.sha256(audio_bytes).hexdigest()
    import io
    import wave

    with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        frames = wav.getnframes()
        raw = np.frombuffer(wav.readframes(frames), dtype=np.float32)
    if channels > 1:
        raw = raw.reshape(-1, channels)[:, 0]
    if rate != 16000:
        ratio = 16000 / rate
        n_out = int(len(raw) * ratio)
        idx = np.arange(n_out) / ratio
        samples = np.interp(idx, np.arange(len(raw)), raw).astype(np.float32)
    else:
        samples = raw.astype(np.float32)
    audio_info = {
        "path": os.path.abspath(args.audio), "sha256": audio_sha,
        "source_rate_hz": int(rate), "channels": int(channels),
        "working_rate_hz": 16000, "valid_samples": int(samples.size),
        "duration_s": round(samples.size / 16000.0, 4),
    }
    report["audio"] = audio_info

    # --- effective decode settings (from the loaded pipeline itself) --------
    model_dir = str(model.get("dir", ""))
    device = str(args.device or "NPU")
    report["model"] = {
        "model": model.get("model"), "precision": model.get("precision"),
        "repo": model.get("repo_id"), "revision": model.get("revision"),
        "dir": model_dir,
    }

    # --- placement verification via the same IR pair the pipeline compiles --
    placement: dict = {"method": "parallel ov.Core compile of encoder/decoder IR on the selected device"}
    try:
        def _compile_probe(xml_name: str) -> str:
            m = core.read_model(os.path.join(model_dir, xml_name))
            if device == "NPU":
                # NPU needs static bounds; mirror the pipeline's batch-1
                # specialization for the probe.
                new_shapes = {}
                for inp in m.inputs:
                    partial = inp.get_partial_shape()
                    dims = [d.get_length() if d.is_static else 1 for d in partial]
                    name = inp.get_any_name()
                    if any(not d.is_static for d in partial):
                        new_shapes[name] = dims
                if new_shapes:
                    m.reshape(new_shapes)
            compiled = core.compile_model(m, device)
            devices_list = str(compiled.get_property("EXECUTION_DEVICES"))
            extra = {}
            if device == "NPU":
                for prop in ("NPU_COMPILER_TYPE", "NPU_COMPILER_VERSION", "NPU_DRIVER_VERSION"):
                    try:
                        extra[prop] = str(compiled.get_property(prop))
                    except Exception as exc:
                        extra[prop] = f"unknown ({exc})"
            del compiled, m
            if extra:
                placement[xml_name + " npu_props"] = extra
            return devices_list
        placement["encoder_execution_devices"] = _compile_probe("openvino_encoder_model.xml")
        placement["decoder_execution_devices"] = _compile_probe("openvino_decoder_model.xml")
    except Exception as exc:
        placement["error"] = str(exc)
    report["placement"] = placement

    # --- load + decode through the production pipeline ----------------------
    started = time.monotonic()
    try:
        pipeline = genai.WhisperPipeline(model_dir, device)
    except Exception as exc:
        code = "OV_NPU_UNAVAILABLE" if device == "NPU" and "NPU" in str(exc).upper() else "OV_MODEL_COMPILE_FAILED"
        report["error"] = {"code": code, "detail": str(exc)}
        _write_report(args.report, report)
        return 1
    load_ms = (time.monotonic() - started) * 1000.0

    gen_config = pipeline.get_generation_config()
    effective = {
        "num_beams": int(gen_config.num_beams), "task": str(gen_config.task),
        "temperature": float(gen_config.temperature),
        "return_timestamps": bool(gen_config.return_timestamps),
        "max_new_tokens": int(gen_config.max_new_tokens),
        "suppress_noise": str(getattr(gen_config, "suppress_tokens", "")),
    }
    report["effective_decode"] = effective

    min_avg = float(model.get("min_avg_logprob", -0.7))
    nsp_threshold = float(model.get("no_speech_threshold", 0.5))

    def decode_once(language: str) -> dict:
        config = pipeline.get_generation_config()
        config.return_timestamps = False
        config.task = "transcribe"
        config.language = language
        t0 = time.monotonic()
        result = pipeline.generate([float(s) for s in samples], config)
        wall_ms = (time.monotonic() - t0) * 1000.0
        semantics = str(getattr(result, "score_semantics", "") or "")
        texts = list(result.texts)
        avg = list(getattr(result, "avg_logprobs", []) or [])
        nsp = list(getattr(result, "no_speech_probs", []) or [])
        summ = list(getattr(result, "sum_logprobs", []) or [])
        counts = list(getattr(result, "token_counts", []) or [])
        finish = list(getattr(result, "finish_reasons", []) or [])
        rows = []
        for i in range(max(1, len(texts))):
            value_avg = avg[i] if i < len(avg) else None
            value_nsp = nsp[i] if i < len(nsp) else None
            accepted = (
                isinstance(value_avg, (int, float)) and value_avg >= min_avg
                and isinstance(value_nsp, (int, float)) and value_nsp < nsp_threshold
            )
            rows.append({
                "window": i, "text": texts[i] if i < len(texts) else "",
                "avg_logprob": value_avg, "no_speech_prob": value_nsp,
                "sum_logprob": summ[i] if i < len(summ) else None,
                "token_count": counts[i] if i < len(counts) else None,
                "finish_reason": finish[i] if i < len(finish) else "",
                "filter": "accepted" if accepted else "rejected",
            })
        perf = getattr(result, "perf_metrics", None)
        stages = {}
        if perf is not None:
            try:
                stages = {
                    "features_ms": round(perf.get_features_extraction_duration(), 3),
                    "encode_ms": round(perf.get_encode_inference_duration(), 3),
                    "decode_ms": round(perf.get_decode_inference_duration(), 3),
                }
            except Exception:
                stages = {}
        return {
            "language": language, "wall_ms": round(wall_ms, 3),
            "rtf": round(wall_ms / 1000.0 / max(0.001, audio_info["duration_s"]), 4),
            "score_semantics": semantics, "rows": rows, "stages_ms": stages,
            "language_source": "forced",
        }

    languages = {"cs+vi": ["cs", "vi"]}.get(args.language, [args.language])
    candidates: dict = {}
    for code in languages:
        candidates[code] = decode_once(code)
    report["candidates"] = candidates

    if args.language == "cs+vi" and len(candidates) == 2:
        missing = [c for c, r in candidates.items()
                   if not r["rows"] or r["rows"][0]["avg_logprob"] is None]
        if missing:
            report["pair"] = {"status": "error", "code": "OV_WHISPER_METRICS_UNAVAILABLE",
                              "missing_candidates": missing}
        else:
            def rank_key(item):
                code, res = item
                row = res["rows"][0]
                return (-row["avg_logprob"], row["no_speech_prob"], code)
            winner = sorted(candidates.items(), key=rank_key)[0][0]
            report["pair"] = {
                "status": "measured", "winner": winner,
                "policy": "highest first avg_logprob, then lower no_speech_prob, then code",
                "pair_wall_ms": round(sum(r["wall_ms"] for r in candidates.values()), 3),
                "first_diverging_frame_index": None,
            }
    else:
        report["pair"] = {"status": "not-applicable"}

    # resident repetition (forced first language) — short-sample estimate
    resident = []
    resident_config = pipeline.get_generation_config()
    resident_config.return_timestamps = False
    resident_config.task = "transcribe"
    resident_config.language = languages[0]
    for _ in range(3):
        t0 = time.monotonic()
        pipeline.generate([float(s) for s in samples], resident_config)
        resident.append(round((time.monotonic() - t0) * 1000.0, 3))
    report["resident_repeats_ms"] = {"samples": resident,
                                     "note": "3 repetitions; short-sample estimate"}

    report["timings_ms"] = {"load_or_compile": round(load_ms, 3),
                            "first_transcription": candidates[languages[0]]["wall_ms"]}
    report["qualification"] = {
        "implementation": "IMPLEMENTED",
        "metrics_contract": ("MEASURED" if any(
            r["score_semantics"] == SCORE_SEMANTICS for r in candidates.values()) else "BLOCKED"),
        "fixed_cs": "MEASURED" if "cs" in candidates else "NOT_RUN",
        "fixed_vi": "MEASURED" if "vi" in candidates else "NOT_RUN",
        "cs_vi_pair": report["pair"].get("status", "not-applicable"),
        "device": device,
    }
    _write_report(args.report, report)

    resolved = {
        "runtime": runtime, "model": model, "audio": audio_info,
        "identity": identity, "effective_decode": effective,
        "device": device, "language": args.language,
    }
    resolved_path = os.path.join(os.path.dirname(os.path.abspath(args.report)),
                                 "resolved-run-inputs.json")
    with open(resolved_path, "w", encoding="utf-8") as handle:
        json.dump(resolved, handle, indent=2, ensure_ascii=False)
    return 0


def _write_report(path: str, report: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


if __name__ == "__main__":
    if "--diagnose" in sys.argv[1:]:
        sys.exit(_diagnose_main(sys.argv[1:]))
    sys.exit(main())
