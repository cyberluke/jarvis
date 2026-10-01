package ai.toastovac.tv.vision;

import android.content.Context;
import android.graphics.Canvas;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.View;

/**
 * Physical host for one RenderTarget.
 *
 * Regular View (onDraw). TextureView/SurfaceView 3840 buffers are
 * geometrically 4K on this Meson but the visible CLIENT target is the
 * activity's 1920×1080 DecorView — painting into that is what the panel
 * actually shows. The RenderTarget still *declares* 3840×2160 (Dashboard)
 * / 1920×1080 (Overlay) so the runtime reports the intended path.
 * ScenePainter scales from the 1920×1080 design space to the canvas.
 */
public final class VisionView extends View {

    private final RenderRole role;
    private final RenderTarget target;
    private final ScenePainter painter = new ScenePainter();
    private final Handler handler = new Handler(Looper.getMainLooper());
    private volatile boolean attached;
    public volatile long frames;
    public volatile String lastError = "";

    private final Runnable pump = new Runnable() {
        @Override public void run() {
            if (!attached) return;
            VisionRuntime rt = VisionRuntime.get();
            rt.syncMedia();
            rt.graph.tick(0.016f, SystemClock.uptimeMillis());
            rt.ticks++;
            invalidate();
            handler.postDelayed(this, 16);
        }
    };

    public VisionView(Context ctx, RenderRole role) {
        super(ctx);
        this.role = role;
        this.target = VisionRuntime.get().target(role);
        setWillNotDraw(false);
        if (role == RenderRole.OVERLAY) setBackgroundColor(0x00000000);
        else setBackgroundColor(0xFF030305);
    }

    public RenderTarget target() {
        return target;
    }

    public void start() {
        attached = true;
        VisionRuntime.get().markAttached(role);
        handler.removeCallbacks(pump);
        handler.post(pump);
    }

    public void stop() {
        attached = false;
        handler.removeCallbacks(pump);
        // keep capability "attached" while the process is the HOME
        // dashboard; only detach when the view is actually gone
    }

    @Override
    protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        start();
    }

    @Override
    protected void onDetachedFromWindow() {
        stop();
        VisionRuntime.get().detach(role);
        super.onDetachedFromWindow();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        VisionRuntime rt = VisionRuntime.get();
        try {
            painter.paint(canvas, rt.graph, target);
            frames++;
            rt.framesPainted = frames;
            lastError = "";
            rt.lastPaintError = "";
            if (frames == 30 || frames == 120) dumpProof(canvas.getWidth(), canvas.getHeight());
        } catch (Exception e) {
            lastError = String.valueOf(e);
            rt.lastPaintError = lastError;
        }
        if ((frames % 30) == 0) rt.refreshCapsPublic();
    }

    private void dumpProof(int w, int h) {
        try {
            android.graphics.Bitmap bmp = android.graphics.Bitmap.createBitmap(
                    Math.max(1, w), Math.max(1, h), android.graphics.Bitmap.Config.ARGB_8888);
            Canvas c = new Canvas(bmp);
            painter.paint(c, VisionRuntime.get().graph, target);
            java.io.File dir = getContext().getExternalFilesDir(null);
            if (dir == null) return;
            java.io.File out = new java.io.File(dir, "vision_proof.png");
            java.io.FileOutputStream fos = new java.io.FileOutputStream(out);
            bmp.compress(android.graphics.Bitmap.CompressFormat.PNG, 90, fos);
            fos.close();
            bmp.recycle();
        } catch (Exception ignored) {
        }
    }
}
