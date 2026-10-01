# 28 — Focused Product Regression (P4)

**Date:** 2026-09-30
**Status:** PASS — native → Korean → Interview → native, decoder createCount = 1 throughout

## Sequence run

```text
native HDR scene → Korean enhanced media → Interview Coach → native HDR scene
```

All transitions driven through the cast control API (cast.scene / cast.youtube /
cast.interviewCoach / cast.scene) at the session rate 59.94 fps; the box stayed
on the single `LiveHdrActivity` (HOME) with one decoder.

## Per-step evidence (results/zero_copy_regression.json)

| Step | source | createCount | drop growth (12 s) | box out fps | audio pkts | orientation | HDR dataspace |
|---|---|---|---|---|---|---|---|
| 1 native scene | native | 1 | 0 | 59.8 | — | VERTICAL_FLIP | BT2020_ITU_PQ |
| 2 Korean (transition) | short | 1 | (in-flight) | 59.7 | 1564 | VERTICAL_FLIP | BT2020_ITU_PQ |
| 2 Korean stable | short | 1 | **0** | 30 (looping 35 s video) | 1746 | VERTICAL_FLIP | BT2020_ITU_PQ |
| 3 Interview Coach | interview | 1 | **0** | — | 2166 | VERTICAL_FLIP | BT2020_ITU_PQ |
| 4 native return | native | 1 | **0** | 59.7 | — | VERTICAL_FLIP | BT2020_ITU_PQ |

## Checks (P4 requirements)

- Native scene: correct orientation (VERTICAL_FLIP), HDR active (BT2020_ITU_PQ),
  full frame — ✓ (before and after the loop).
- Korean media: original Korean audio flows (TZHL STREAM_AUDIO PCM; pkts grow
  1564 → 1746), Czech subtitles burned into the composite, enhanced playback
  decision path intact (PLAY_NOW) — ✓.
- Interview Coach: scene renders after switch, transitions work — ✓
  (createCount 1, no drop growth, HDR layer back on native return).
- Return to native: no black screen (client connected throughout, state
  RUNNING), correct orientation, HDR returns — ✓.
- Across all transitions: **decoder createCount = 1** — ✓.

## Transition drop storm fixed (root cause)

Initial run showed a real defect: on every source switch the decoder dropped
hundreds of frames (up to 937 ≈ 15 s frozen) as "too late". Root cause: the
video PTS timeline resets to 0 at each switch while the box's AudioTrack clock
kept its previous timeline; `DecoderSession.drain()` then compared new-stream
frames (pts ≈ 0) against the stale audio clock (minutes ahead) → every frame
`deltaMs < -200` → dropped. Fix (box side, `AudioSession` + `DecoderSession`):
the audio clock only gates video when the audio was (re)configured AFTER the
current video stream started (`configureEpoch` captured at each video
FLAG_CONFIG). Audio-less sources (native scene, interview) no longer gate on a
stale clock. Result: drop storm gone; the only remaining drops are a bounded
one-time A/V lock-in transient (~18 frames) in the first seconds of Korean
playback while the audio clock re-anchors — steady state shows zero growth.

## Hard invariants across the loop

```text
decoder createCount = 1   ✓ (every step)
readbackBytesPerFrame = 0 ✓
orientation correct        ✓ (VERTICAL_FLIP throughout)
HDR returns                ✓ (BT2020_ITU_PQ at 1 and 4)
no black screen            ✓ (client connected, decoder RUNNING at every step)
```

## Files / artifacts

- `tools/live_hdr/regression_run.py` — deterministic regression runner.
- `results/zero_copy_regression.json` — full per-step evidence.
- `android/.../live/AudioSession.java`, `live/DecoderSession.java` — audio-
  coherence drop-gate fix; APK rebuilt + installed.