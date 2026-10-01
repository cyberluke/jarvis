# 07 — Autonomous evaluation

## Defects found
1. Live mux treated each NAL as an AU; `00 01` false start codes.
2. Incremental scene blob broadcast crash (`blob[..., None]`).
3. `Producer.stop` shadowed `threading.Event` → `'Event' object is not callable`.
4. Interview last turn unanswered at the 75 s cutoff.
5. Desktop QSV rejected nv12 / forced 4K gdigrab.

## Fixes applied
AU assembler + CONFIG/IDR; scene blit fix; rename `Producer._stop`;
drop unanswered interview turn; desktop P010 letterbox.

## Reruns
- Live HDR after AU fix → UnBlank + BT2020_ITU_PQ
- Interview 70 s → all checks pass
- Desktop after P010 fix → producer_alive + UnBlank
- Korean Short after Event-shadow fix → source=short ~11 Mbps

## Remaining top issue
Live HEVC path is video-only. Korean audio and a true subtitle overlay
need either a software mix into the encode or a non-decoder overlay
(CLIENT 1080p), not a second HEVC decoder.
