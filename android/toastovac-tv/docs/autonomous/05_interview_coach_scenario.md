# 05 — Interview Coach

## Goal
Timed Python interview ≥60 s through a real turn state machine.

## Implementation
`src/jarvis/everywhere/interview_coach.py`

- Events: INTERVIEW_STARTED / QUESTION / CANDIDATE_TRANSCRIPT_PARTIAL /
  CANDIDATE_TRANSCRIPT_FINAL / EVALUATION / COACH_HINT / INTERVIEW_ENDED
- Injection at `InterviewBroker.inject_final` (Whisper-final boundary)
- Deterministic evaluator (catches the planted generator mistake)
- TV notes ≤ 25 words

## First run (defect)
75.0 s, 5 turns, last turn unanswered → `each_answer_evaluated=false`.

## Fix + rerun
Drop unanswered trailing turn. Second run:

```text
duration_sec = 70.0
turns        = 4
checks       = all true
TV notes     = ["Solid. Next question.",
                "Stop. The body does not run on call — only when iterated.",
                "Solid. Next question.",
                "Solid. Next question."]
```

## Unproven
No dedicated TV interview scene painted this night (text lives in the event
log + JSON). `cast.interviewCoach` is wired on the host; the scene producer
is still the live HDR canvas.

## Artifacts
`results/interview_coach_result.json`
`logs/interview_1a640b6606.jsonl`
`logs/interview_system_eval.json`
