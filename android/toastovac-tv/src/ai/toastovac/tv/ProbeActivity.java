package ai.toastovac.tv;

import android.app.Activity;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.os.Bundle;
import android.util.Log;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.WindowManager;

/**
 * 4K SurfaceProbe — phase 1.
 *
 * Question: can an explicitly sized native Surface (3840x2160) exist on this
 * box even though the WMS UI is clamped to 1920x1080 (config_maxUiWidth) and
 * SurfaceFlinger has ro.surface_flinger.max_graphics_height=1080?
 *
 * We render a true 3840x2160 one-pixel alternating stripe pattern. If the
 * panel physically shows 1px stripes (not upscaled mush), and the SF layer
 * dump shows a 3840x2160 BufferQueue, the vendor clamp is bypassable.
 */
public class ProbeActivity extends Activity implements SurfaceHolder.Callback {

    static final String TAG = "TOASTOVAC4K";
    static final int PW = 3840;
    static final int PH = 2160;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        Log.e(TAG, "ProbeActivity created, requesting SurfaceView " + PW + "x" + PH);
        SurfaceView sv = new SurfaceView(this);
        setContentView(sv);
        SurfaceHolder h = sv.getHolder();
        h.setFormat(PixelFormat.RGBA_8888);
        h.setFixedSize(PW, PH);
        h.addCallback(this);
    }

    @Override
    public void surfaceCreated(SurfaceHolder h) {
        Log.e(TAG, "surfaceCreated frame=" + h.getSurfaceFrame());
    }

    @Override
    public void surfaceChanged(SurfaceHolder h, int format, int width, int height) {
        Log.e(TAG, "SURFACE=" + width + "x" + height + " frame=" + h.getSurfaceFrame());
        drawPattern(h);
    }

    private void drawPattern(SurfaceHolder h) {
        try {
            Canvas c = h.lockHardwareCanvas();
            if (c == null) {
                Log.e(TAG, "lockHardwareCanvas returned null");
                return;
            }
            c.drawColor(Color.BLACK);
            Paint p = new Paint();
            p.setStrokeWidth(1f);
            // 1 physical-pixel alternating vertical stripes — cannot be
            // reproduced from a 1080p upscale.
            for (int x = 0; x < PW; x++) {
                p.setColor((x & 1) == 0 ? Color.WHITE : Color.BLACK);
                c.drawLine(x, 0, x, PH, p);
            }
            // orientation markers: red top-left, blue top-right, green bottom-left
            Paint m = new Paint();
            m.setColor(Color.RED);
            c.drawRect(0, 0, 48, 48, m);
            m.setColor(Color.BLUE);
            c.drawRect(PW - 48, 0, PW, 48, m);
            m.setColor(Color.GREEN);
            c.drawRect(0, PH - 48, 48, PH, m);
            h.unlockCanvasAndPost(c);
            Log.e(TAG, "PATTERN drawn " + PW + "x" + PH + " (1px stripes + markers)");
        } catch (Exception e) {
            Log.e(TAG, "draw failed: " + e);
        }
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder h) {
        Log.e(TAG, "surfaceDestroyed");
    }
}