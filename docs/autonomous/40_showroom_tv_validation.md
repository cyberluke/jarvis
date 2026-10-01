# 40 — SHOWROOM TV Validation

Date: 2026-10-01 · Clip: `tbsY9zC-w6U` · 65" Samsung, HOMATICS Box R 4K Plus

## Demo assets on TV

`showroom_compare.mp4` — full-screen sequential demo (25 s, loops), same 5 s window per segment:

```
1. ORIGINAL SDR   (SDR appearance in HDR transport — dark on HDR display, honest reference)
2. CINEMATIC      (CINEMATIC_V1 = current ladder: cin_color)
3. SHOWROOM A
4. SHOWROOM B
5. SHOWROOM C
```

Each segment: identical source frames, same crop/scale/timing, portrait 1215x2160 centered on 3840x2160, small unobtrusive label. No split-screen (Phase 12).

Instant switching (no re-render): `POST /cast {"action":"cast.showroom","name":"compare|A|B|C"}` streams `enhanced/showroom_<name>.mp4`.

## Verified TV state (compare streaming)

| Field | Value |
|---|---|
| source | showroom:tbsY9zC-w6U:compare |
| HDR current type | **HDR10-GAMMA_ST2084** |
| boxOut | 60.1–60.3 |
| sustained drops | 0 (1292→1292 over 10 s) |
| inputFull | 1/0 (transient, 0 dropped) |
| createCount | 1 |
| clients | 1 |

## Decision (user)

Awaiting user verdict: **KEEP A / KEEP B / KEEP C / LOWER / REMOVE**.

Success criterion (Phase 13): at sofa distance, `CINEMATIC → SHOWROOM_B` must be immediately recognizable within ~1 s. If "did anything change?" → FAIL.

## After winner selection (Phase 16)

Render only the winner for the full clip: `enhanced/cin_showroom.mp4` (from `cin_detail.mp4`, ~20 s at ~107 fps) + optional `ab_showroom.mp4` (CINEMATIC vs SHOWROOM), re-verify TV, then keep both product profiles:

```
CINEMATIC_V1 = restrained / natural / film-like
SHOWROOM_V1  = vivid / punchy / premium TV demo
```