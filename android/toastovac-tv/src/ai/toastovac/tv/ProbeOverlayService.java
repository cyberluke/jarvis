package ai.toastovac.tv;

import android.app.Service;
import android.content.Intent;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.os.IBinder;
import android.util.Log;
import android.view.Gravity;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.WindowManager;

/**
 * 4K SurfaceProbe — phase 2.
 *
 * Same test inside a TYPE_APPLICATION_OVERLAY window (the future companion
 * overlay mode). Requires SYSTEM_ALERT_WINDOW via
 *   adb shell appops set ai.toastovac.tv SYSTEM_ALERT_WINDOW allow
 */
public class ProbeOverlayService extends Service implements SurfaceHolder.Callback {

    static final String TAG = "TOASTOVAC4K";
    static final int PW = 3840;
    static final int PH = 2160;

    private SurfaceView view;
    private WindowManager wm;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (view != null) return START_STICKY;
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        view = new SurfaceView(this);

        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.RGBA_8888);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.setTitle("toastovac-4k-probe");

        try {
            wm.addView(view, lp);
        } catch (Exception e) {
            Log.e(TAG, "overlay addView failed: " + e);
            stopSelf();
            return START_NOT_STICKY;
        }

        SurfaceHolder h = view.getHolder();
        h.setFormat(PixelFormat.RGBA_8888);
        h.setFixedSize(PW, PH);
        h.addCallback(this);
        Log.e(TAG, "ProbeOverlayService overlay window added, surface requested " + PW + "x" + PH);
        return START_STICKY;
    }

    @Override
    public void surfaceCreated(SurfaceHolder h) {
        Log.e(TAG, "overlay surfaceCreated frame=" + h.getSurfaceFrame());
    }

    @Override
    public void surfaceChanged(SurfaceHolder h, int format, int width, int height) {
        Log.e(TAG, "overlay SURFACE=" + width + "x" + height + " frame=" + h.getSurfaceFrame());
        try {
            Canvas c = h.lockHardwareCanvas();
            if (c == null) return;
            c.drawColor(Color.BLACK);
            Paint p = new Paint();
            p.setStrokeWidth(1f);
            for (int x = 0; x < PW; x++) {
                p.setColor((x & 1) == 0 ? Color.WHITE : Color.BLACK);
                c.drawLine(x, 0, x, PH, p);
            }
            Paint m = new Paint();
            m.setColor(Color.RED);
            c.drawRect(0, 0, 48, 48, m);
            m.setColor(Color.BLUE);
            c.drawRect(PW - 48, 0, PW, 48, m);
            m.setColor(Color.GREEN);
            c.drawRect(0, PH - 48, 48, PH, m);
            h.unlockCanvasAndPost(c);
            Log.e(TAG, "overlay PATTERN drawn " + PW + "x" + PH);
        } catch (Exception e) {
            Log.e(TAG, "overlay draw failed: " + e);
        }
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder h) {
        Log.e(TAG, "overlay surfaceDestroyed");
    }

    @Override
    public void onDestroy() {
        if (view != null && wm != null) {
            try {
                wm.removeView(view);
            } catch (Exception ignored) {
            }
        }
        view = null;
        super.onDestroy();
    }
}