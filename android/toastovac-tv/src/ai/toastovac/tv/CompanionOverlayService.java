package ai.toastovac.tv;

import android.app.Service;
import android.content.Intent;
import android.graphics.PixelFormat;
import android.os.IBinder;
import android.util.Log;
import android.view.Gravity;
import android.view.WindowManager;

import ai.toastovac.tv.vision.RenderRole;
import ai.toastovac.tv.vision.VisionRuntime;
import ai.toastovac.tv.vision.VisionView;

/**
 * Companion Overlay — transparent RGBA over whatever is under Toastovač
 * (Kodi / YouTube / another app). Paints the SAME scene graph as HOME,
 * but the physical target is 1920×1080 with alpha. Do not request 4K
 * here: Meson CLIENT composition downsamples it anyway.
 */
public class CompanionOverlayService extends Service {

    static final String TAG = "TOASTOVAC-OVERLAY";
    static final String ACTION_STOP = "ai.toastovac.tv.STOP_OVERLAY";

    private VisionView view;
    private WindowManager wm;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (view != null) return START_STICKY;

        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        view = new VisionView(this, RenderRole.OVERLAY);

        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.MATCH_PARENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                        | WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                        | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN
                        | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.setTitle("toastovac-companion");

        try {
            wm.addView(view, lp);
        } catch (Exception e) {
            Log.e(TAG, "addView failed: " + e);
            stopSelf();
            return START_NOT_STICKY;
        }
        view.start();
        Log.i(TAG, "overlay window 1920x1080 alpha");
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        if (view != null) view.stop();
        VisionRuntime.get().detach(RenderRole.OVERLAY);
        if (view != null && wm != null) {
            try { wm.removeView(view); } catch (Exception ignored) {}
        }
        view = null;
        super.onDestroy();
    }
}
