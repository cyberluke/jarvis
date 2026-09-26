# OpenVINO / Intel NPU speech backend — provenance and status

Prepared 2026-09-24 against `develop` @ `dbf1f59`. Source completion and
demonstrated runtime readiness are different deliverables; both are recorded
separately below.

## Runtime contract (owner-supplied, authoritative for the custom build)

| Item | Value |
|---|---|
| Default installer root | `C:\Program Files\Intel` (editable in the NSIS wizard; a hint, not proof) |
| Registry key / value | `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Intel` → `InstallDir` (both registry views are checked) |
| Native libraries | `<root>\runtime\bin\intel64\Release` |
| Python package | `<root>\python` |
| Environment helpers | `<root>\setupvars.bat` / `setupvars.ps1` (child environment is constructed equivalently; the parent is not mutated) |
| CMake package | `<root>\runtime\cmake\openvino-config.cmake` |
| Supplied Python binding | `_pyopenvino.cp313-win_amd64.pyd` |
| Supplied Core wheel | `openvino-2026.5.0-383-cp313-cp313-win_amd64.whl` |
| Supplied GenAI wheel | `openvino_genai-2026.4.0.0-cp313-cp313-win_amd64.whl` |
| GenAI source commit (actual checkout) | `f62ebcf0` — the Core build number does not establish the GenAI implementation; recorded separately |
| Owner's source checkout | `D:\_SATIN_AI_2\openvino-master-lab\openvino` (developer reference, not a production default) |
| Owner's staging install | `D:\_SATIN_AI_2\openvino-master-lab\ov-install` (developer reference) |

Observed at inspection time (2026-09-24, this machine): the staging tree
`ov-install` imports under CPython 3.13 x64 and reports Core
`2026.4.0-1-7c4d2733dc0` with devices `CPU, GPU.0, GPU.1`; the `2026.5.0-383`
wheel is present in `build-ov\wheels`. The loaded value is compared to the
selected manifest and printed at startup; it is never assumed.

## Model catalog (immutable revisions, resolved 2026-09-24)

| Canonical model | INT8 repository @ revision | FP16 repository @ revision | INT8 / FP16 bytes |
|---|---|---|---|
| tiny | `OpenVINO/whisper-tiny-int8-ov` @ `a850762d9724` | `OpenVINO/whisper-tiny-fp16-ov` @ `44662d68573b` | 48 798 585 / 84 345 023 |
| base | `OpenVINO/whisper-base-int8-ov` @ `0606293f0511` | `OpenVINO/whisper-base-fp16-ov` @ `84fbe975a79a` | 84 713 802 / 154 240 654 |
| small | `OpenVINO/whisper-small-int8-ov` @ `5b831719e093` | `OpenVINO/whisper-small-fp16-ov` @ `2410d022171c` | 256 761 816 / 493 214 244 |
| medium | `OpenVINO/whisper-medium-int8-ov` @ `8d43cce84672` | `OpenVINO/whisper-medium-fp16-ov` @ `eddc8397f55a` | 784 027 062 / 1 538 858 732 |
| large-v3 | `OpenVINO/whisper-large-v3-int8-ov` @ `e197310d1fe6` | `OpenVINO/whisper-large-v3-fp16-ov` @ `220761e60602` | 1 568 411 870 / 3 099 051 222 |
| large-v3-turbo | `OpenVINO/whisper-large-v3-turbo-int8-ov` @ `b568445dd5dc` | `OpenVINO/whisper-large-v3-turbo-fp16-ov` @ `0250c28d68c7` | 828 096 445 / 1 627 657 898 |

Required assets per snapshot (actual repository layout; no CTranslate2
`model.bin`, no decoder-with-past naming assumed): `config.json`,
`generation_config.json`, `preprocessor_config.json`, `tokenizer.json`,
`tokenizer_config.json`, `vocab.json`, `merges.txt`, `added_tokens.json`,
`special_tokens_map.json`, `normalizer.json`, `openvino_encoder_model.xml/.bin`,
`openvino_decoder_model.xml/.bin`, `openvino_tokenizer.xml/.bin`,
`openvino_detokenizer.xml/.bin`. Cache identity:
`<whisper_cache_dir>/openvino/<model>-<precision>-<revision12>`.
`no_speech_token_id` is resolved from the model's own
`generation_config.json`, falling back to the documented multilingual default
`1` (token `<|nospeech|>`).

## Score semantics (`ov-genai-whisper-logprob-v1`)

- `avg_logprob = sum_logprob / (token_count + 1)` over the hypothesis actually
  returned, matching the faster-whisper/CTranslate2 and OpenAI reference
  convention (generated tokens plus one). Generated-token accounting includes
  EOS and excludes the SOT/task prefix.
- `sum_logprob`: greedy decoding reports the cumulative log probability
  directly; beam search recovers it from the length-penalised sequence score
  by reversing the penalty (`score · n^length_penalty`). The search score
  itself stays in `scores`.
- `no_speech_prob`: probability of the no-speech token from the raw decoder
  distribution at the start-of-transcript position — full-vocabulary softmax
  in FP32 before suppression/language masking. Forced-language runs use a
  dedicated SOT-only decoder request on the same device with correctly reset
  state; automatic runs reuse the same SOT inference that performs language
  detection. Continuation windows report `0.0`, matching the OpenAI reference.
- `cs+vi`: two forced decodes (task transcribe) over the same preprocessed
  audio with identical model/precision/policy/semantics; existing first-row
  ranking with deterministic ties; only the winner enters the existing
  filters and language-aware postprocessing.

## Companion extension (GenAI source, matched to the installed wheel)

Patched files in `D:\_SATIN_AI_2\openvino-master-lab\openvino.genai`:

- `src/cpp/include/openvino/genai/whisper_pipeline.hpp` — genuine per-window
  statistics on the public result.
- `src/cpp/include/openvino/genai/automatic_speech_recognition/pipeline.hpp` —
  same fields on `ASRDecodedResults`.
- `src/cpp/include/openvino/genai/automatic_speech_recognition/generation_config.hpp`
  and `src/cpp/include/openvino/genai/whisper_generation_config.hpp` —
  `no_speech_token_id`.
- `src/cpp/src/whisper/whisper.hpp`, `whisper.cpp` — statistics collection,
  SOT probe, no-speech softmax.
- `src/cpp/src/whisper/models/decoder.hpp/.cpp` — `detect_language` returns
  the SOT no-speech probability; `probe_no_speech_prob` for forced language.
- `src/cpp/src/whisper/pipeline.cpp`, `pipeline_static.cpp` — real scores and
  statistics replace the constant `1.f` (stateful and static NPU paths).
- `src/cpp/src/automatic_speech_recognition/models/whisper/pipeline.hpp` —
  adapter copies the fields.
- `src/python/py_whisper_pipeline.cpp`, `src/python/py_asr_pipeline.cpp` —
  fields exposed to Python.

Rebuild the companion against the selected Core CMake package
(`runtime\cmake\openvino-config.cmake`) so CMake/pip cannot resolve a
different Core; the Core itself does not need a rebuild for this change.
Expected artifacts: the rebuilt `openvino_genai` wheel + `py_openvino_genai`
pyd for cp313, and a regenerated `py_openvino_genai.pyi`.

## Status

| Deliverable | State |
|---|---|
| Source changes (jarvis + GenAI companion) | COMPLETE (this checkout) |
| Rebuilt companion wheel/pyd for the patched source | NOT_RUN — still the pre-patch `openvino_genai-2026.4.0.0` wheel |
| NPU execution on the owner's machine | NOT_RUN — the staging install exposes `CPU, GPU.0, GPU.1`; the NPU plugin DLL is not present in `ov-install\runtime\bin\intel64\Release`, so `NPU` currently yields `OV_NPU_UNAVAILABLE` |
| Czech/Vietnamese audio + latency comparison (NPU INT8 vs FP16 vs faster-whisper) | NOT_RUN |

Until the companion is rebuilt, the worker reports score capability
`legacy-constant-1f`; the adapter then blocks recognition with
`OV_WHISPER_METRICS_UNAVAILABLE` instead of feeding fabricated values into
the existing hallucination filters.

## Error codes

| Code | Meaning |
|---|---|
| `OV_RUNTIME_NOT_FOUND` | No validated installation (explicit root, registry views, and the default hint all failed or conflict) |
| `OV_RUNTIME_ABI_MISMATCH` | No CPython 3.13 x64 interpreter for the supplied cp313 binding |
| `OV_COMPANION_MISSING` | Core imports but GenAI/tokenizer components do not |
| `OV_RUNTIME_VERSION_MISMATCH` | Loaded Core differs from the selected manifest (reported, not assumed) |
| `OV_NPU_UNAVAILABLE` | Selected NPU device absent from the runtime device list; no hidden CPU fallback |
| `OV_MODEL_INCOMPLETE` | Required IR/tokenizer assets missing, zero-size, or download interrupted (resumable) |
| `OV_MODEL_COMPILE_FAILED` | Pipeline compilation failed on the selected device |
| `OV_DECODE_POLICY_UNSUPPORTED` | Requested decode option not supported by the installed pipeline |
| `OV_WHISPER_METRICS_UNAVAILABLE` | Genuine `avg_logprob` / `no_speech_prob` absent or invalid; fixed-language choice is not a cure |
| `OV_WHISPER_PAIR_UNSUPPORTED` | Metrics work, but the two-pass `cs+vi` contract is unavailable; choose `cs` or `vi` explicitly |

## Production lane (sealed 2026-09-25)

| Item | Value |
|---|---|
| Lane | Custom OpenVINO GenAI 2026.4 cp314 (NPU), `whisper_openvino_runtime_source = "python"` |
| Interpreter | `C:\Python314\python.exe` (CPython 3.14.7 x64, cp314 ABI) |
| OpenVINO core | 2026.4.0-22959-99c81491cc3-releases/2026/4 (devices CPU, GPU.0, GPU.1, NPU) |
| GenAI companion | 2026.4.0.0-3398-f62ebcf04f2, source worktree `genai-2026.4-wt` @ `f62ebcf0` |
| Package path | `C:\Python314\Lib\site-packages\openvino_genai` |
| Native hashes (sha256, first 16) | `py_openvino_genai.cp314-win_amd64.pyd` B291952806EB1F1A · `openvino_genai.dll` 8E034A8EC16BEE07 · `openvino_tokenizers.dll` E8ABEE608A7B02AA · core `openvino.dll` F3AC0C1F31788EFA |
| Device | NPU (stateful pipeline, greedy `num_beams = 1`) |
| Static mode | `whisper_openvino_pipeline = "static"` — explicit diagnostic only (`STATIC_PIPELINE=True`, decoder KV capacity 448); never an automatic fallback |
| Score contract | `ov-genai-whisper-logprob-v1`: per-window `sum_logprobs`, `token_counts`, `avg_logprobs` (`avg = sum / (count + 1)`, faster-whisper semantics), `no_speech_probs` (raw SOT-position softmax), `finish_reasons`, `kv_cache_exhausted` |
| Model (observed) | `large-v3-turbo` INT8 @ `b568445dd5dc` → `D:\_MODELS\openvino\large-v3-turbo-int8-b568445dd5dc` |
| Overhead | One persistent worker, model loaded once; per-utterance decode only (NPU warm ≈ 1.5–1.9 s for a 3.18 s clip, RTF ≈ 0.48–0.61) |

Build provenance: the tokenizers target additionally includes the OpenVINO
frontend header directories (`src/frontends/*/include`) because the runtime
include dir does not ship them; this is a build-time include fix only.

## Fallback policy

No silent fallback. The selected lane (interpreter, source, device, pipeline)
is fixed at startup and echoed as the effective configuration. NPU absent or
native failure ⇒ structured error code, lane DEGRADED/FAILED, no automatic
CPU/static/PyPI substitution; the next run requires an explicit operator
selection.

## Known upstream limitation (contained)

Whisper beam search (`num_beams > 1`) is broken upstream in the tested
2026.4/2026.5 family (unpatched PyPI wheel: `ValueError: vector too long`;
patched cp314: AV `0xC0000005`). The custom metadata patch did not
introduce it. Jarvis rejects `num_beams > 1` deterministically **before**
the native call with `OV_DECODE_POLICY_UNSUPPORTED`; greedy decoding is the
production contract.

## Production seal (2026-09-25)

Startup decomposition (worker phases, structured `ov-worker: phase` log
lines; process-spawn measured by the parent):

| Phase | Cold cache | Warm cache |
|---|---|---|
| process spawn | ~0.5 s | ~0.5 s |
| `python_import_ms` | 219–464 ms | 212–235 ms |
| `openvino_core_init_ms` | ~0.8 ms | ~0.7 ms |
| `device_discovery_ms` | 130–261 ms | 131–155 ms |
| first handshake (NPU driver first-touch) | ~12.7 s | ~12.6 s |
| `pipeline_construct_ms` | 310 769 ms (compile) | 4 287–10 201 ms (cache import) |
| first decode (graph upload) | 2.2–3.0 s | 2.5 s |

Persistent compiled-model cache (Gate C1, proven):

- mechanism: NPU plugin property `CACHE_DIR` (per-device `RW` in
  `SUPPORTED_PROPERTIES`; the global `OPTIMIZATION_CACHE_PATH` name is not
  supported by this build). Passed as a keyword to `WhisperPipeline` and
  forwarded to every NPU `compile_model`.
- cache root: `.cache/openvino-npu/<model>-<device>-<pipeline>-<core-build>`
  (repository `.cache` convention). One identity per model revision,
  device, pipeline mode and core build; the plugin embeds the core build in
  its index and invalidates mismatches itself. A miss recompiles on the same
  device; a cache problem never switches device or backend.
- evidence: cold compile 310.8 s vs fresh-process cached import 4.3 s with
  two blobs (1.34 GB + 0.71 GB); identical decode text and native metadata
  after import.

Handshake identity validation (parent side, `OVSpeechWorker.validate_identity`):
interpreter path, `openvino_genai.__file__`, NPU presence, score-semantics
capability and pipeline mode are checked before any native decode; a
mismatch is an explicit failure and poisons the worker (no alternate lane).

Beam guard: `num_beams=2` → `OV_DECODE_POLICY_UNSUPPORTED` before the
worker request; native beam code never invoked.

VoiceListener test baseline: the 72 failures were the 4-arg constructor
calls in `tests/test_voice_listener.py` against the 5-arg production
signature (`presence_coordinator`; `daemon.py` is the reference caller).
Test call sites updated to the real API; suite now 100/100. Remaining
unrelated: `test_hot_window_input.py` 3 failures (60 with the HEAD
fixture) — pre-existing, outside this pass.
