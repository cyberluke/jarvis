# 18 — Whisper persistence verification (P1)

## State

`src/jarvis/everywhere/whisper_worker.py` is a **process singleton**
(`get_worker()` → module-level `_WORKER`; `ensure_loaded()` loads once and
reuses the model for every job). This was built in a prior run and is now
verified — **not rewritten** (per the run instruction).

Lifecycle:

```text
UNLOADED → LOADING → READY ⇄ BUSY
```

- model: `large-v3-turbo` (faster-whisper, `D:\_MODELS` cache)
- device: `auto`, compute: `int8` (unchanged; no RTX move)
- cast_host and `korean_short` both route through `get_worker()`

## Persistence proof (one process, 3 jobs, 105 s audio)

```text
WHISPER_WORKER_LOADED large-v3-turbo 4.2s load_count=1
WHISPER_WORKER_JOB 1 wall=8.18s rtf=0.078
WHISPER_WORKER_JOB 2 wall=5.86s rtf=0.056
WHISPER_WORKER_JOB 3 wall=5.70s rtf=0.054
```

- modelLoadCount: **1** (pass: =1)
- jobCount: **3** (pass: >=3)
- warm job latency: 5.7–8.2 s; RTF 0.054–0.078
- process RAM: 26.8 MB before load → 798.2 MB after 3 jobs (one resident model)
- device/compute recorded by the worker: auto / int8; `rtxUsed: false`

The 306 s cold load cost is paid once per process; every later job in the same
process reuses the resident model.

## Evidence

- `results/whisper_persistence.json`
- worker status via `get_worker().status()` and `/cast/status.whisperState`

## Known scope

Persistence is per-process. cast_host is its own Python process, so a brand-new
cast_host still pays one model load — the singleton guarantees it pays it
exactly once per process lifetime.