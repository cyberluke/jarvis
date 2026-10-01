"""4K HDR scene: linear nits → PQ Rec.2020 RGB16. Rasterized at 3840×2160.

Incremental raster: the static layer (background, hairlines, ramp, wordmark,
chips) is rendered once. Each live frame copies the static layer and redraws
only the dynamic regions (clock text, Spark, moving highlight). This keeps
the scene genuinely live (clock ticks, Spark moves) at useful fps on CPU
without lowering resolution or switching SDR.
"""
from __future__ import annotations

import math
import time
from datetime import datetime

import numpy as np

W, H = 3840, 2160
DESIGN_W, DESIGN_H = 1920, 1080
SCALE = 2.0

_TEXT_CACHE: dict = {}


def pq_eotf_inv(nits):
    n = np.clip(np.asarray(nits, dtype=np.float64) / 10000.0, 0.0, 1.0)
    m1, m2 = 2610.0 / 16384.0, 2523.0 / 32.0
    c1, c2, c3 = 3424.0 / 4096.0, 2413.0 / 128.0, 2392.0 / 128.0
    ym = np.power(n, m1)
    return np.power((c1 + c2 * ym) / (1.0 + c3 * ym), m2)


def nits_rgb(r, g, b):
    return pq_eotf_inv(r), pq_eotf_inv(g), pq_eotf_inv(b)


def fill_rect(img, x0, y0, x1, y1, rgb_nits):
    x0 = max(0, int(x0))
    y0 = max(0, int(y0))
    x1 = min(W, int(x1))
    y1 = min(H, int(y1))
    if x1 <= x0 or y1 <= y0:
        return
    r, g, b = nits_rgb(*rgb_nits)
    img[y0:y1, x0:x1, 0] = r
    img[y0:y1, x0:x1, 1] = g
    img[y0:y1, x0:x1, 2] = b


def draw_text_mask(text, size, color_nits):
    """Cache text → (mask, rgb16) so repeated clock renders are cheap."""
    key = (text, size, tuple(color_nits))
    hit = _TEXT_CACHE.get(key)
    if hit is not None:
        return hit
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("arial.ttf", size)
    except Exception:
        font = ImageFont.load_default()
    tmp = Image.new("L", (8, 8), 0)
    d = ImageDraw.Draw(tmp)
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0] + 8, bbox[3] - bbox[1] + 8
    im = Image.new("L", (max(tw, 8), max(th, 8)), 0)
    ImageDraw.Draw(im).text((4, 2), text, fill=255, font=font)
    mask = np.asarray(im, dtype=np.float64) / 255.0
    rgb = np.array(nits_rgb(*color_nits), dtype=np.float64)
    hit = (mask, rgb)
    _TEXT_CACHE[key] = hit
    return hit


def blit_mask(img, x, y, mask, rgb):
    h, w = mask.shape
    x, y = int(x), int(y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x or y1 <= y:
        return
    sl = mask[: y1 - y, : x1 - x][..., None]
    img[y:y1, x:x1] = img[y:y1, x:x1] * (1.0 - sl) + rgb * sl


class LiveScene:
    """Live 4K PQ scene with cached static layer + per-frame dynamic regions."""

    def __init__(self):
        self._static = None  # uint16 (H,W,3) RGB48 static layer
        self._build_static()

    def _build_static(self):
        img = np.zeros((H, W, 3), dtype=np.float64)
        fill_rect(img, 0, 0, W, H, (0.04, 0.04, 0.05))

        # 1 px diagnostic hairlines (physical pixels)
        img[0, :, :] = nits_rgb(80, 80, 80)
        img[:, 0, :] = nits_rgb(80, 80, 80)
        for i in range(0, 96, 2):
            img[24:120, 24 + i, :] = nits_rgb(203, 203, 203)

        # dark-to-bright PQ ramp
        ramp = np.linspace(0.02, 400.0, 3200)
        pq = pq_eotf_inv(ramp)
        img[1960:2040, 320:3520, 0] = pq
        img[1960:2040, 320:3520, 1] = pq
        img[1960:2040, 320:3520, 2] = pq

        # wordmark
        mask, rgb = draw_text_mask("Toastovač", 176, (180, 180, 200))
        blit_mask(img, (W - mask.shape[1]) // 2, 860, mask, rgb)

        # static label
        mask, rgb = draw_text_mask("live 4K60  PQ  BT.2020  Main10", 36, (0, 140, 130))
        blit_mask(img, (W - mask.shape[1]) // 2, 1180, mask, rgb)

        # cyan / lavender chips
        fill_rect(img, 120, 160, 420, 360, (0, 160, 140))
        fill_rect(img, W - 420, 160, W - 120, 360, (130, 70, 200))

        img = np.clip(img, 0.0, 1.0)
        self._static = np.round(img * 65535.0).astype(np.uint16)

    def render(self, t: float, hud: dict | None = None) -> np.ndarray:
        """Return RGB16 (H,W,3) uint16 PQ-encoded Rec.2020 frame."""
        img = self._static.copy()

        # clock (changes each second)
        clock = datetime.now().strftime("%H:%M:%S")
        mask, rgb = draw_text_mask(clock, 72, (160, 160, 175))
        blit_mask(img, (W - mask.shape[1]) // 2, 1080, mask, rgb)

        # Spark: traveling highlight along bottom line
        phase = (t * 0.18) % 1.0
        sx = int(W * (0.22 + 0.56 * phase))
        sy = 1880
        yy, xx = np.ogrid[sy - 80 : sy + 80, sx - 80 : sx + 80]
        if 0 <= sy - 80 and sy + 80 <= H and 0 <= sx - 80 and sx + 80 <= W:
            d = np.sqrt((yy - sy) ** 2 + (xx - sx) ** 2)
            spark = np.clip(1.0 - d / 70.0, 0, 1) ** 2
            f = img[sy - 80 : sy + 80, sx - 80 : sx + 80].astype(np.float64)
            f[..., 0] += spark * pq_eotf_inv(700) * 65535.0
            f[..., 1] += spark * pq_eotf_inv(160) * 65535.0
            f[..., 2] += spark * pq_eotf_inv(860) * 65535.0
            img[sy - 80 : sy + 80, sx - 80 : sx + 80] = np.clip(f, 0, 65535).astype(np.uint16)

        # moving HDR highlight blob
        hx = int(W * (0.55 + 0.18 * math.sin(t * 0.7)))
        hy = int(H * (0.32 + 0.06 * math.cos(t * 0.9)))
        y0, y1 = max(0, hy - 90), min(H, hy + 90)
        x0, x1 = max(0, hx - 90), min(W, hx + 90)
        yy, xx = np.ogrid[y0:y1, x0:x1]
        d = np.sqrt((yy - hy) ** 2 + (xx - hx) ** 2)
        blob = np.clip(1.0 - d / 80.0, 0, 1) ** 2
        f = img[y0:y1, x0:x1].astype(np.float64)
        f[..., 0] += blob * pq_eotf_inv(900) * 65535.0
        f[..., 1] += blob * pq_eotf_inv(900) * 65535.0
        f[..., 2] += blob * pq_eotf_inv(900) * 65535.0
        img[y0:y1, x0:x1] = np.clip(f, 0, 65535).astype(np.uint16)

        if hud:
            line = "  ".join(f"{k}={v}" for k, v in hud.items())
            mask, rgb = draw_text_mask(line[:90], 28, (90, 90, 110))
            blit_mask(img, 40, H - 70, mask, rgb)

        return img


def render_frame(t: float, hud: dict | None = None) -> np.ndarray:
    """Backward-compatible entry point (module-level LiveScene singleton)."""
    return _SINGLETON.render(t, hud)


_SINGLETON = LiveScene()