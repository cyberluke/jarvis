# 08 — Korean Short audio + measured A/V

## Goal
Original Korean speech on HDMI, Czech subtitles, one HEVC decoder.

## Implementation
- TZHL v2: header `stream u16` (`VIDEO=0`, `AUDIO=1`). v1 reserved=0 stays video.
- PC: second ffmpeg `-re` decode of `source.mp4` audio → PCM S16LE 48 kHz stereo, 40 ms packets.
- PCM held until first video IDR so both share the mux `pts0`.
- Android: process-singleton `AudioSession` / `AudioTrack` (~80 ms buffer).
- `DecoderSession.drain()` records `video_pts - audio_clock`.

## First run (defect)
Audio started during scene-to-short switch, before first Short IDR.
`avAvg=157 ms` (video ahead of audio clock). Watchable but outside 80 ms.

## Fix + rerun
Hold AUDIO_FRAME until `video_origin_us` (first IDR). Shrink AudioTrack buffer.

```text
rate=48000 ch=2
pkts=1249 queued=4796160 underruns=0
avAvg=-15  avP50=-15  avP95=14  avMax=68  n=776
creates=1  drop=0
HwcVideo--65536 UnBlank  DEVICE
```

## Proven
Korean PCM on TV, Czech burned-in SRT, HwcVideo UnBlank, decoder #1 only,
|A/V| p50 15 ms / max 68 ms (target <80 ms).

## Unproven
HDMI speaker confirmation is box-side AudioTrack + continuous write with
zero underruns (no TV-mic recording).
