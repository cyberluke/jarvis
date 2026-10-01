# 12 — Desktop capture upgrade

## Probe
```text
gdigrab  yes
dshow    yes
ddagrab  no   (this ffmpeg 7.1 build)
WGC helper  not in repo
```

## Decision
Keep `gdigrab` → letterbox 3840×2160 P010 → hevc_qsv. Do not add a second
desktop-only engine. Desktop remains honest 1080p SDR.

## Status
PARTIAL — backend not upgraded; product path already proven last night.
