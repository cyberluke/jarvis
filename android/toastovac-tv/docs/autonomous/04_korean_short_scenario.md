# 04 — Korean Short + Czech subtitles

## Goal
`cast.youtube` → download → Toastovač Whisper (Korean) → Czech subtitles → TV.

## Source
```text
https://www.youtube.com/watch?v=x6uD7GeAj84
Korean with Joy 조이랑 한국어
35 s, 3840×2160 VP9 + opus, Korean speech, public, not login-gated
```

## Path used
```text
yt_download.YouTubeDownloader.start  (existing Toastovač job/cache)
korean_short.transcribe_korean       (faster-whisper large-v3-turbo, language=ko)
korean_translate.translate_cues      (jarvis.reply.engine.chat_with_messages)
cast_host POST /cast action=cast.youtube
ffmpeg composite + burned SRT → hevc_qsv → TZHL → singleton decoder
```

## Whisper snapshot
```text
implementation = faster-whisper 1.2.1
model          = large-v3-turbo
cache          = D:\_MODELS (mobiuslabsgmbh/faster-whisper-large-v3-turbo)
device         = auto / int8
load           = 306 s (cold)
transcribe     = 1.98 s for 35.3 s audio  RTF=0.056
```

## Proven
Download cached; 8 Korean cues; 8 Czech SRT cues with preserved timing;
`cast.youtube` switched the live source; HwcVideo UnBlank; ~11 Mbps.

## Unproven / honest gaps
- Burned-in subtitles (cannot add a second HEVC/overlay decoder).
- Live HEVC path is video-only; Korean audio remains on `source.mp4`.
- Sync not instrumented beyond SRT timestamps + ffmpeg `-re`.
- SDR content inside 4K Main10 bt709 — not tagged as PQ.

## Artifacts
See `results/korean_short_result.json`.
