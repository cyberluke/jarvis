# 19 — Interview Coach on the actual TV (P2)

## Product path

```text
POST /cast {"action":"cast.interviewCoach","topic":"Python","durationSec":85}
→ InterviewTvSession (InterviewBroker event machine)
→ InterviewScene (SceneGraph state → 4K PQ raster → hevc_qsv Main10)
→ TZHL :8768 → c2.amlogic.hevc.decoder (createCount=1) → HwcVideo DEVICE
```

The broker is the only state writer; the scene painter only consumes the
shared TV state (event-driven, no arbitrary backend painting).

## Event protocol wired to the scene

`INTERVIEW_STARTED → QUESTION → CANDIDATE_TRANSCRIPT_PARTIAL →
CANDIDATE_TRANSCRIPT_FINAL → EVALUATION → COACH_HINT → NEXT_QUESTION →
INTERVIEW_ENDED`

The broker's `inject_final()` is the same boundary a real Whisper final
transcript uses; the timed simulation injects through it.

## Candidate profile exercised (P2.5)

The question/answer bank contains the required mix and each was exercised:

| turn | profile | coach note |
|---|---|---|
| 1 list/tuple | strong answer | "Solid. Next question." |
| 2 generator | subtle factual mistake (body "starts running on call") | "Stop. The body does not run on call — only when iterated." |
| 3 async/await | incomplete ("await just sleeps the thread") | "Good. Add: event loop." |
| 4 decorator | overlong answer | "Solid. Next question." |

## Pass criteria (P2.7) — all true

```text
wall_clock_75s          : true  (85.0 s)
four_turns              : true  (4 turns)
notes_short             : true  (max note 11 words; hard max 30)
questions_changed       : true  (5 distinct questions)
listening_state_changed : true  (CANDIDATE_TRANSCRIPT_PARTIAL seen)
coach_after_answer      : true  (COACH_HINT after FINAL, EVALUATION between)
coach_min_hold_2_5s     : true  (every hint visible 4.0 s)
no_duplicate_transitions: true  (0)
no_stale_question       : true  (0)
rendered_through_toastovac: true
status                  : pass
```

## Readability measurement (P2.6)

```text
question_visible_sec : [18.0, 18.0, 18.0, 18.01, 12.98]
coach_visible_sec    : [4.0, 4.0, 4.0, 4.0]     (deterministic 4 s hold)
transition_count     : 24
duplicate_transition_count : 0
stale_question_count : 0
```

## TV rendering evidence

- host: `src=interview:Python`, `idr` advancing, `clients=1`
- box: `ai.toastovac.tv/.LiveHdrActivity` focused; SurfaceView layer
  `w/h:4096x2304`, `dataspace:0x10c10000`; decoder `c2.amlogic.hevc.decoder`
  `createCount=1` (never a second instance)
- `rendered_on_box` set by the host's render watchdog (idr advanced with a
  connected client during the session)

## Robustness fix this run

The headset pairing probe (`resolve_interview_headset`) no longer aborts the
TV scene when no live headset is present — the simulated transcript-final
boundary drives the scene regardless (`INTERVIEW_HEADSET_UNAVAILABLE` warning).

## Evidence

- `results/interview_coach_tv.json` (status: pass)
- `results/interview_coach_tv_result.json` (same content)
- host log `stat src=interview:Python ...` lines
- box SurfaceFlinger layer dump