# 09 — Persistent Whisper worker

## Ownership
`src/jarvis/listening/listener.py` already loads `faster-whisper` into the
daemon `Listener.model`. Cast used a one-shot `WhisperModel()` in
`korean_short.py` (306 s cold).

## Change
`src/jarvis/everywhere/whisper_worker.py` — process singleton:

```text
UNLOADED → LOADING → READY ⇄ BUSY
```

`korean_short.transcribe_korean` and `cast_host` go through `get_worker()`.
Model stays resident. Device remains `auto` / `int8`. No RTX switch.

## Measured (warm HF cache, same process)

```text
ensure_loaded  8.04 s wall / worker_load_sec 4.79
load_count     1
job1           2.13 s  RTF 0.060
job2           0.69 s  RTF 0.019
job3           0.65 s  RTF 0.018
inference      3
rtx_used       false
```

Second/third Korean Shorts do not pay another 306 s load in this process.

## Unproven
Cross-process daemon sharing (cast_host is its own Python process; first
load in a new process still pays model init).
