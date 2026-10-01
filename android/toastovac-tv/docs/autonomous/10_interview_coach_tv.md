# 10 — Interview Coach TV scene

## Surface
`cast.interviewCoach` → `InterviewTvSession` events → shared `TV` state →
`InterviewScene` (4K PQ, Spark, cyan/lavender) → hevc_qsv → TZHL →
singleton decoder.

Android `SceneGraph` / `ScenePainter` also gained interview fields so a
future overlay-off dashboard can paint the same hierarchy.

## Constraints
Question wrapped to 3 lines. Coach notes one sentence, ≤ 25 words.
Full evaluation stays in JSONL.

## Timed run (70.0 s wall-clock)

```text
turns=4  inject_final boundary
1 strong   list/tuple     → "Solid. Next question."
2 mistake  generator      → "Stop. The body does not run on call — only when iterated."
3 incomplete async        → "Good. Add: event loop."
4 overlong decorator      → "Solid. Next question."
HwcVideo UnBlank  createCount=1
```

## Self-eval
Previous question replaced. Coach note held across the eval sleep.
No duplicate QUESTION events. Overlong spoken answer did not leak onto TV.

## Unproven
No framebuffer screenshot of HwcVideo (plane is not CLIENT). Evidence is
event log + last TV state + UnBlank.
