# 17 — Subtitle / translation quality (P0)

## No-silent-fallback (P0.1)

The translation module (`src/jarvis/everywhere/korean_translate.py`) contains
**no second provider path**. Every request goes through
`chat_with_messages(cfg, ...)` → `get_llm_backend(cfg)` → the configured
`llm_provider` / `llm_base_url` / `llm_chat_model`.

- configured provider: `openai_compatible` → `http://192.168.1.155:8002/v1`
  (LM Studio), model `gemma4-26b`
- actual provider this run: **LM_STUDIO** (= configured; no fallback)
- if the configured provider is unavailable the stage raises
  `TranslationProviderError`; the cast action records
  `translationStatus = PROVIDER_UNAVAILABLE`, `actualProvider = NONE` and does
  not emit a partial SRT. No provider is inferred.

## Provider identity record (P0.2)

Every translation writes `subtitle_quality.json` (fixture copy + cast-side
`/cast/status.subtitle`):

```text
provider / model / endpoint / request count / source language /
target language / cue count / repair count
```

No API keys are recorded.

## Deterministic validator (P0.3)

`validate_czech_cue()` — no LLM: non-empty text, valid Unicode (no control
chars), no Cyrillic, no Korean/Hangul residue, no duplicate consecutive cue
text, letter/digit presence, timestamps (`start >= 0`, `end > start`).

## Repair (P0.4)

Invalid cues are repaired with the **same** configured provider/model via a
narrow single-cue request; `maxCueRepairAttempts = 2`; still-invalid cues are
marked and never emitted as garbage.

## Media-end handling (P0.5)

`sanitize_media_end()`: cues with `start >= mediaDuration` are dropped, ends
beyond the duration are clamped, collapsed cues are dropped; counts recorded
(`clampedCueCount`, `droppedCueCount`, `cuePastMediaEndCount`). Cached SRTs
also get a deterministic hygiene pass at cast time
(`apply_media_end_to_srt`), so a stale cache can never show past-end cues.

## Fixture re-run (s8B3_Nq2Cgk, 105 s)

```text
translationStatus : OK
configuredProvider: LM_STUDIO   actualProvider: LM_STUDIO
model             : gemma4-26b  endpoint: http://192.168.1.155:8002/v1
requestCount      : 3 (one per 10-cue batch)
cueCount          : 25 (26 input - 1 dropped)
Cyrillic cues     : 0   (was 2 in the Ollama-fallback SRT)
Korean residue    : 0
cue past media end: 0   (1 dropped; start 105.69 >= 105.333)
repairs           : 0
dropped           : 1   clamped: 0
media duration    : 105.333 s (enhanced derivative)
```

Result: the TV subtitle track is clean Czech — no Cyrillic artifacts, no cue
past the end of the video, and the translation itself is more faithful
(e.g. „řidičský průkaz v šatní skříni" for 장롱면허 instead of the fallback's
„krabičková licence").

## Evidence

- `results/subtitle_quality.json`
- fixture copy: `%LOCALAPPDATA%/VIVERRA/Toastovac/videos/s8B3_Nq2Cgk/subtitle_quality.json`
- `/cast/status.subtitle` (translationProvider / translationStatus / state)