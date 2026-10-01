"""4K HDR scene on the GPU (OpenGL via moderngl).

The SceneGraph semantic state (clock, Spark, blob, text, hud) is rendered by
fragment shaders on the Intel GPU. The static layer (background, chips, PQ
ramp, hairlines, wordmark) is composed once into a texture; every live frame
draws the static texture plus the dynamic elements in one fullscreen pass and
reads back PQ-encoded Rec.2020 RGB48 — the exact byte format LiveScene
produces, so the encoder/transport path is unchanged.

API is LiveScene-compatible: ``render(t, hud=None) -> uint16 (H,W,3)``.
"""
from __future__ import annotations

import math
import time
from datetime import datetime

import numpy as np

from hdr_scene import W, H, pq_eotf_inv, draw_text_mask  # noqa: F401

_VS = """
#version 330
in vec2 in_pos;
out vec2 v_uv;
void main() {
    v_uv = in_pos * 0.5 + 0.5;
    gl_Position = vec4(in_pos, 0.0, 1.0);
}
"""

_FS = """
#version 330
in vec2 v_uv;
out uvec4 out_color;

uniform sampler2D u_static;
uniform sampler2D u_clock;
uniform sampler2D u_hud;
uniform vec2 u_res;
uniform float u_t;
uniform vec2 u_clock_pos;   // pixel origin (x, y)
uniform vec2 u_clock_size;
uniform vec2 u_hud_pos;
uniform vec2 u_hud_size;
uniform vec3 u_clock_color;
uniform vec3 u_hud_color;
uniform vec3 u_spark_color;
uniform vec3 u_blob_color;

float pq(float n) {
    n = clamp(n / 10000.0, 0.0, 1.0);
    float m1 = 2610.0 / 16384.0;
    float m2 = 2523.0 / 32.0;
    float c1 = 3424.0 / 4096.0;
    float c2 = 2413.0 / 128.0;
    float c3 = 2392.0 / 128.0;
    float ym = pow(n, m1);
    return pow((c1 + c2 * ym) / (1.0 + c3 * ym), m2);
}

float sample_tex(sampler2D tex, vec2 pos, vec2 size, vec2 uv) {
    if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) return 0.0;
    vec2 p = (gl_FragCoord.xy - pos) / max(size, vec2(1.0));
    p.y = 1.0 - p.y;  // texture v is flipped
    if (p.x < 0.0 || p.x > 1.0 || p.y < 0.0 || p.y > 1.0) return 0.0;
    return texture(tex, p).r;
}

uint q16(float v) { return uint(clamp(v, 0.0, 1.0) * 65535.0 + 0.5); }

void main() {
    vec3 c = texture(u_static, v_uv).rgb;
    float ca = sample_tex(u_clock, u_clock_pos, u_clock_size, vec2(1.0));
    c = mix(c, u_clock_color, ca);
    float ha = sample_tex(u_hud, u_hud_pos, u_hud_size, vec2(1.0));
    c = mix(c, u_hud_color, ha);

    // Spark: traveling highlight along the bottom line (same math as CPU)
    float phase = mod(u_t * 0.18, 1.0);
    float sx = u_res.x * (0.22 + 0.56 * phase);
    float sy = 1880.0;
    float d = distance(gl_FragCoord.xy, vec2(sx, sy));
    float spark = pow(clamp(1.0 - d / 70.0, 0.0, 1.0), 2.0);
    c += spark * u_spark_color;

    // Moving HDR highlight blob
    float hx = u_res.x * (0.55 + 0.18 * sin(u_t * 0.7));
    float hy = u_res.y * (0.32 + 0.06 * cos(u_t * 0.9));
    float d2 = distance(gl_FragCoord.xy, vec2(hx, hy));
    float blob = pow(clamp(1.0 - d2 / 80.0, 0.0, 1.0), 2.0);
    c += blob * u_blob_color;

    c = clamp(c, 0.0, 1.0);
    out_color = uvec4(q16(c.r), q16(c.g), q16(c.b), 1u);
}
"""


def _as_tex(ctx, arr: np.ndarray, components: int, dtype: str):
    return ctx.texture((arr.shape[1], arr.shape[0]), components, data=arr.tobytes(), dtype=dtype)


class GLScene:
    """GPU-composited 4K PQ scene, LiveScene-compatible output."""

    def __init__(self):
        import moderngl
        self.ctx = moderngl.create_standalone_context()
        self.prog = self.ctx.program(vertex_shader=_VS, fragment_shader=_FS)
        # fullscreen triangle strip (NDC corners)
        quad = np.array([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0], dtype="f4")
        self.vbo = self.ctx.buffer(quad.tobytes())
        self.vao = self.ctx.vertex_array(self.prog, [(self.vbo, "2f", "in_pos")])
        self._static_tex = None
        self._clock_tex = None
        self._clock_key = ""
        self._hud_tex = None
        self._hud_key = ""
        self._fbo = self.ctx.framebuffer(
            color_attachments=[self.ctx.texture((W, H), 4, dtype="u2")])
        self._build_static()
        self._set_uniforms()

    def _set_uniforms(self):
        p = self.prog
        p["u_res"].value = (float(W), float(H))
        p["u_spark_color"].value = tuple(float(x) for x in pq_eotf_inv(np.array([700.0, 160.0, 860.0])))
        b = pq_eotf_inv(np.array([900.0, 900.0, 900.0]))
        p["u_blob_color"].value = tuple(float(x) for x in b)
        p["u_clock_color"].value = tuple(float(x) for x in pq_eotf_inv(np.array([160.0, 160.0, 175.0])))
        p["u_hud_color"].value = tuple(float(x) for x in pq_eotf_inv(np.array([90.0, 90.0, 110.0])))

    def _build_static(self):
        """One-time static layer: reuse the proven CPU compositor, then GPU."""
        from hdr_scene import LiveScene
        static_uint16 = LiveScene()._static
        f4 = static_uint16.astype(np.float32) / 65535.0
        self._static_tex = _as_tex(self.ctx, f4, 3, "f4")
        self.prog["u_static"].value = 0

    def _update_clock(self):
        clock = datetime.now().strftime("%H:%M:%S")
        if clock == self._clock_key:
            return
        self._clock_key = clock
        mask, _rgb = draw_text_mask(clock, 72, (160, 160, 175))
        a = (mask * 255.0).astype(np.uint8)
        if self._clock_tex is not None:
            self._clock_tex.release()
        self._clock_tex = _as_tex(self.ctx, a, 1, "u1")
        self.prog["u_clock"].value = 1
        self.prog["u_clock_size"].value = (float(mask.shape[1]), float(mask.shape[0]))
        self.prog["u_clock_pos"].value = (
            float((W - mask.shape[1]) // 2), float(1080))

    def _update_hud(self, hud):
        if not hud:
            return
        line = "  ".join(f"{k}={v}" for k, v in hud.items())[:90]
        if line == self._hud_key:
            return
        self._hud_key = line
        mask, _rgb = draw_text_mask(line, 28, (90, 90, 110))
        a = (mask * 255.0).astype(np.uint8)
        if self._hud_tex is not None:
            self._hud_tex.release()
        self._hud_tex = _as_tex(self.ctx, a, 1, "u1")
        self.prog["u_hud"].value = 2
        self.prog["u_hud_size"].value = (float(mask.shape[1]), float(mask.shape[0]))
        self.prog["u_hud_pos"].value = (40.0, float(H - 70 - mask.shape[0]))

    def render(self, t: float, hud: dict | None = None) -> np.ndarray:
        self._update_clock()
        self._update_hud(hud)
        p = self.prog
        p["u_t"].value = float(t)
        self._fbo.use()
        self._fbo.clear(0, 0, 0, 1)
        self.vao.render(mode=self.ctx.TRIANGLE_STRIP)
        data = self._fbo.read(components=4, dtype="u2")
        # raw uint16 RGB48 (alpha dropped) — zero numpy conversion
        return np.frombuffer(data, dtype=np.uint16).reshape(H, W, 4)[:, :, :3]

    def close(self):
        try:
            self.ctx.release()
        except Exception:
            pass


def create_scene():
    """GPU scene when available, else the proven CPU scene (no silent break)."""
    try:
        return GLScene()
    except Exception as e:
        print("GPU_SCENE_UNAVAILABLE", type(e).__name__, e, flush=True)
        from hdr_scene import LiveScene
        return LiveScene()


def render_frame(t: float, hud: dict | None = None) -> np.ndarray:
    return _GL_SINGLETON.render(t, hud)


_GL_SINGLETON = None


def _singleton():
    global _GL_SINGLETON
    if _GL_SINGLETON is None:
        _GL_SINGLETON = create_scene()
    return _GL_SINGLETON