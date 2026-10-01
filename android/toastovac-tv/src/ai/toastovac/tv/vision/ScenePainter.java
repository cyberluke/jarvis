package ai.toastovac.tv.vision;

import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.PorterDuff;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.Typeface;

import java.util.Calendar;
import java.util.Locale;

/**
 * Single painter for Dashboard and Overlay. Scales design space → buffer.
 * QLED-safe: near-black field, accent only in 2–6 px (design) lines.
 */
public final class ScenePainter {

    private static final int VOID = 0xFF030305;
    private static final int TEXT = 0xF0F6F4FF;
    private static final int MUTED = 0x94DCD8EB;
    private static final int LAV = 0xFFA78BFA;
    private static final int AQUA = 0xFF55E6D0;

    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint stroke = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF tmp = new RectF();

    public ScenePainter() {
        text.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        text.setSubpixelText(true);
        stroke.setStyle(Paint.Style.STROKE);
        stroke.setStrokeCap(Paint.Cap.ROUND);
    }

    public void paint(Canvas c, SceneGraph g, RenderTarget t) {
        // If the canvas is not the declared buffer (DecorView fallback),
        // scale from the actual clip so text is never off-canvas.
        int cw = c.getWidth();
        int ch = c.getHeight();
        if (cw > 0 && ch > 0) t = t.withBuffer(cw, ch);
        float sx = t.scaleX;
        float sy = t.scaleY;
        boolean fillIdle = t.opaqueBackground && !g.videoMounted;
        if (fillIdle) {
            c.drawColor(VOID);
        } else {
            c.drawColor(Color.TRANSPARENT, PorterDuff.Mode.CLEAR);
        }

        if (g.veil()) {
            fill.setColor(t.opaqueBackground ? 0x47030305 : 0x33030305);
            c.drawRect(0, 0, t.bufferW, t.bufferH, fill);
        }

        if (g.interviewQuestion != null && !g.interviewQuestion.isEmpty()) {
            drawInterview(c, g, t, sx, sy);
        } else if (fillIdle) {
            drawIdle(c, g, t, sx, sy);
        } else if (g.videoMounted || t.opaqueBackground) {
            drawChrome(c, g, t, sx, sy);
        } else {
            drawClock(c, t, sx, sy, true);
        }

        drawSpark(c, g, t, sx, sy);
        drawVoice(c, g, t, sx, sy);
        drawToast(c, g, t, sx, sy);

        // target badge — always on, so a glance at the TV proves the path
        drawBadge(c, t, sx, sy);
        if (t.role == RenderRole.DASHBOARD) {
            draw4kDiagnostic(c, g, t);
        }
    }

    private void drawInterview(Canvas c, SceneGraph g, RenderTarget t, float sx, float sy) {
        if (t.opaqueBackground) c.drawColor(VOID);
        text.setTextAlign(Paint.Align.LEFT);
        text.setColor(AQUA);
        text.setTextSize(22f * sy);
        String mode = g.interviewMode.isEmpty() ? "PYTHON INTERVIEW" : g.interviewMode;
        c.drawText(mode, 80f * sx, 72f * sy, text);
        text.setTextAlign(Paint.Align.RIGHT);
        text.setColor(MUTED);
        text.setTextSize(22f * sy);
        c.drawText(g.interviewProgress, t.bufferW - 80f * sx, 72f * sy, text);
        text.setTextAlign(Paint.Align.LEFT);
        text.setColor(TEXT);
        text.setTextSize(36f * sy);
        float y = t.bufferH * 0.38f;
        String q = g.interviewQuestion;
        int max = 48;
        int start = 0;
        int lines = 0;
        while (start < q.length() && lines < 3) {
            int end = Math.min(q.length(), start + max);
            if (end < q.length()) {
                int sp = q.lastIndexOf(' ', end);
                if (sp > start) end = sp;
            }
            c.drawText(q.substring(start, end).trim(), 80f * sx, y, text);
            y += 48f * sy;
            start = end;
            lines++;
        }
        text.setColor(LAV);
        text.setTextSize(22f * sy);
        c.drawText(g.interviewPhase, 80f * sx, y + 16f * sy, text);
        if (g.interviewCoach != null && !g.interviewCoach.isEmpty()) {
            text.setColor(AQUA);
            text.setTextSize(26f * sy);
            c.drawText(g.interviewCoach, 80f * sx, t.bufferH - 80f * sy, text);
        }
    }

    private void drawIdle(Canvas c, SceneGraph g, RenderTarget t, float sx, float sy) {
        text.setTextAlign(Paint.Align.CENTER);
        text.setColor(TEXT);
        text.setTextSize(88f * sy);
        text.setFakeBoldText(true);
        c.drawText(g.brand, t.bufferW * 0.5f, t.bufferH * 0.46f, text);
        text.setFakeBoldText(false);

        text.setTextSize(28f * sy);
        text.setColor(MUTED);
        c.drawText(g.status, t.bufferW * 0.5f, t.bufferH * 0.54f, text);

        text.setTextSize(22f * sy);
        text.setColor(0x663C4050);
        c.drawText(g.hint, t.bufferW * 0.5f, t.bufferH * 0.60f, text);

        drawClock(c, t, sx, sy, false);
    }

    private void drawChrome(Canvas c, SceneGraph g, RenderTarget t, float sx, float sy) {
        if (g.title != null && !g.title.isEmpty()) {
            text.setTextAlign(Paint.Align.LEFT);
            text.setColor(TEXT);
            text.setTextSize(28f * sy);
            c.drawText(g.title, 80f * sx, 64f * sy, text);
        }
        drawClock(c, t, sx, sy, true);

        if (g.seek01 > 0f) {
            float x0 = 80f * sx;
            float x1 = t.bufferW - 80f * sx;
            float y = t.bufferH - 56f * sy;
            stroke.setStrokeWidth(2f * sy);
            stroke.setColor(0x55A78BFA);
            c.drawLine(x0, y, x1, y, stroke);
            stroke.setColor(LAV);
            stroke.setStrokeWidth(3f * sy);
            c.drawLine(x0, y, x0 + (x1 - x0) * g.seek01, y, stroke);
        }
    }

    private void drawClock(Canvas c, RenderTarget t, float sx, float sy, boolean corner) {
        Calendar cal = Calendar.getInstance();
        String clock = String.format(Locale.US, "%02d:%02d",
                cal.get(Calendar.HOUR_OF_DAY), cal.get(Calendar.MINUTE));
        text.setColor(MUTED);
        if (corner) {
            text.setTextAlign(Paint.Align.RIGHT);
            text.setTextSize(26f * sy);
            c.drawText(clock, t.bufferW - 80f * sx, 64f * sy, text);
        } else {
            text.setTextAlign(Paint.Align.CENTER);
            text.setTextSize(22f * sy);
            c.drawText(clock, t.bufferW * 0.5f, t.bufferH * 0.38f, text);
        }
    }

    private void drawSpark(Canvas c, SceneGraph g, RenderTarget t, float sx, float sy) {
        float y = t.bufferH - 28f * sy;
        float x0 = t.bufferW * 0.38f;
        float x1 = t.bufferW * 0.62f;
        stroke.setShader(new LinearGradient(x0, y, x1, y,
                new int[]{0x00A78BFA, LAV, AQUA, 0x0055E6D0},
                null, Shader.TileMode.CLAMP));
        stroke.setStrokeWidth(2f * sy);
        c.drawLine(x0, y, x1, y, stroke);
        stroke.setShader(null);

        float px = x0 + (x1 - x0) * g.sparkPhase;
        fill.setColor(LAV);
        c.drawCircle(px, y, 3.5f * sy, fill);

        // tiny chevron above the line (Toastovač mark)
        stroke.setColor(LAV);
        stroke.setStrokeWidth(2f * sy);
        float mid = (x0 + x1) * 0.5f;
        c.drawLine(mid - 6f * sx, y - 10f * sy, mid, y - 16f * sy, stroke);
        c.drawLine(mid, y - 16f * sy, mid + 6f * sx, y - 10f * sy, stroke);
    }

    private void drawVoice(Canvas c, SceneGraph g, RenderTarget t, float sx, float sy) {
        if (g.voiceCaption == null || g.voiceCaption.isEmpty()) return;
        if (g.state != SceneGraph.ShellState.LISTENING
                && g.state != SceneGraph.ShellState.THINKING
                && g.state != SceneGraph.ShellState.SPEAKING
                && g.state != SceneGraph.ShellState.RESPONDING
                && g.state != SceneGraph.ShellState.PAUSED) {
            return;
        }
        text.setTextAlign(Paint.Align.CENTER);
        text.setColor(TEXT);
        text.setTextSize(40f * sy);
        c.drawText(g.voiceCaption, t.bufferW * 0.5f, t.bufferH * 0.48f, text);
        if (g.voiceStatus != null && !g.voiceStatus.isEmpty()) {
            text.setTextSize(22f * sy);
            text.setColor(MUTED);
            c.drawText(g.voiceStatus, t.bufferW * 0.5f, t.bufferH * 0.54f, text);
        }
    }

    private void drawToast(Canvas c, SceneGraph g, RenderTarget t, float sx, float sy) {
        if (g.toast == null || g.toast.isEmpty()) return;
        text.setTextAlign(Paint.Align.CENTER);
        text.setTextSize(26f * sy);
        float tw = text.measureText(g.toast);
        float padX = 28f * sx;
        float padY = 16f * sy;
        float cx = t.bufferW * 0.5f;
        float cy = 120f * sy;
        tmp.set(cx - tw * 0.5f - padX, cy - 28f * sy - padY,
                cx + tw * 0.5f + padX, cy + padY);
        fill.setColor(0xC70C0A14);
        c.drawRoundRect(tmp, 18f * sy, 18f * sy, fill);
        text.setColor(TEXT);
        c.drawText(g.toast, cx, cy, text);
    }

    private void drawBadge(Canvas c, RenderTarget t, float sx, float sy) {
        String label = t.role == RenderRole.DASHBOARD
                ? "DASH 4K DEVICE LOOP"
                : "OVERLAY 1920×1080 α";
        text.setTextAlign(Paint.Align.LEFT);
        text.setTextSize(16f * sy);
        text.setColor(0x88A78BFA);
        c.drawText(label, 24f * sx, t.bufferH - 12f * sy, text);
    }

    /**
     * Native-pixel diagnostics, drawn in PHYSICAL canvas pixels (not design
     * units) so sharpness can be judged directly on the TV at true 4K:
     *  - 1 px hairline along the top and left edges
     *  - 1 px alternating black/white column chart (96 px wide)
     *  - 2×2 px checkerboard block
     *  - live codec pipeline status
     */
    private void draw4kDiagnostic(Canvas c, SceneGraph g, RenderTarget t) {
        int cw = c.getWidth();
        int ch = c.getHeight();
        if (cw <= 0) return;

        // 1 px hairlines (crisp only if the panel is true 4K)
        stroke.setStrokeWidth(1f);
        stroke.setColor(0x66FFFFFF);
        c.drawLine(0f, 0.5f, cw, 0.5f, stroke);
        c.drawLine(0.5f, 0f, 0.5f, ch, stroke);

        // 1 px alternating column chart — the classic 4K sharpness check
        int bx = 24;
        int by = 24;
        int bw = 96;
        fill.setColor(0xFFFFFFFF);
        for (int i = 0; i < bw; i++) {
            if ((i & 1) == 0) {
                c.drawRect(bx + i, by, bx + i + 1, by + bw, fill);
            }
        }
        // 2×2 px checkerboard (4K pixel grid)
        int cb = bx + bw + 16;
        fill.setColor(0xFFFFFFFF);
        for (int i = 0; i < 16; i++) {
            for (int j = 0; j < 16; j++) {
                if (((i + j) & 1) == 0) {
                    c.drawRect(cb + i * 2, by + j * 2, cb + i * 2 + 2, by + j * 2 + 2, fill);
                }
            }
        }

        // live pipeline status (physical px, mono)
        VisionRuntime rt = VisionRuntime.get();
        String[] lines = {
                "4K UI LOOP " + (rt.caps.pipelineRunning ? "RUN" : "STOP"),
                rt.caps.encoderName + " → " + rt.caps.decoderName,
                rt.caps.codecMime + " enc " + rt.caps.encodedW + "x" + rt.caps.encodedH
                        + " dec " + rt.caps.decodedW + "x" + rt.caps.decodedH,
                "fps " + rt.caps.pipelineFps + " submit " + rt.caps.framesSubmitted
                        + " drop " + rt.caps.framesDropped,
                "render " + String.format(java.util.Locale.US, "%.1f", rt.caps.renderMs)
                        + "ms enc " + String.format(java.util.Locale.US, "%.1f", rt.caps.encLatencyMs)
                        + "ms e2e " + String.format(java.util.Locale.US, "%.1f", rt.caps.e2eMs) + "ms",
                rt.caps.pipelineReason
        };
        text.setTextAlign(Paint.Align.LEFT);
        text.setTextSize(24f);
        text.setColor(0xB0A78BFA);
        float y = ch - 40f;
        for (String line : lines) {
            c.drawText(line, 24f, y, text);
            y -= 30f;
        }
    }
}
