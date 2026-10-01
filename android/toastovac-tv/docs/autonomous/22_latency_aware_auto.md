# 22 — Latency-aware media AUTO (P5)

## Separation of concerns (P5.1)

Quality eligibility and interactive latency policy are now separate:

```text
PLAY_NOW          - cached derivative if present, else play ORIGINAL
                    immediately when the predicted cost exceeds the
                    interactive budget; optionally schedule the derivative
                    for later (background cache).
PREPARE_CINEMATIC - wait for the enhancement (quality wins).
BACKGROUND_CACHE  - build the derivative without playback intent.
```

`cast.youtube` accepts `intent` and `quality` in the request body; explicit
quality requests (`cinematic`, `enhance`, `upscale`, `best`) map to
`PREPARE_CINEMATIC` (P5.4). Everything else is `PLAY_NOW`.

## Processing-time estimator (P5.2)

Deterministic, no LLM. Uses measured media-per-wall rates from real runs:
720p→1440p = 0.331 (105 s in 317.3 s), 1080p→2160p = 0.117 (3.93 s in 33.7 s).
`estimatedProcessingSec` and `actualProcessingSec` are recorded on every
enhancement record. On the 105 s fixture the estimate was **318.2 s vs
317.3 s actual** (0.3% error).

## Interactive threshold (P5.3)

`interactive_enhancement_budget_sec` (default **10 s**) is a real config
field (`src/jarvis/config.py`), read by `intel_enhance._interactive_budget_sec`
— no scattered hardcodes.

## Verified behavior

```text
PLAY_NOW, uncached 720p 105 s:
  decision=PASS_THROUGH_FOR_LATENCY  playbackSource=ORIGINAL
  reason="predicted processing cost 318.2s exceeds interactive budget 10.0s"
  backgroundCacheScheduled=true      ← derivative builds in the background

PLAY_NOW, cached 720p:
  decision=ENHANCE playbackSource=ENHANCED (cache wins over latency)

PREPARE_CINEMATIC, uncached 720p 4 s:
  decision=ENHANCE playbackSource=ENHANCED audioMode=COPY
  estimate 12.1 s / actual 29.4 s
```

This is a **product policy**, not an enhancement failure — the cast never
waits minutes and never looks frozen for `PLAY_NOW`.

## Fix found during testing

Two enhancement jobs sharing one directory (background cache + a new cast)
collided on the `_tmp_<preset>.mp4` temp name ("Cannot create a file when
that file already exists"). The temp file name is now unique per job
(`_tmp_<preset>_<pid>_<uuid>.mp4`).

## Evidence

- `results/latency_aware_auto.json`
- `/cast/status` fields: `intent`, `estimatedEnhancementSec`,
  `enhancementDecision`, `enhancementStage`