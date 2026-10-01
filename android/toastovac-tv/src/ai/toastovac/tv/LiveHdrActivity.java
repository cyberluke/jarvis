package ai.toastovac.tv;

import android.app.Activity;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Display;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.WindowManager;
import android.widget.FrameLayout;

import ai.toastovac.tv.live.BoxStatsReporter;
import ai.toastovac.tv.live.DecoderOwner;
import ai.toastovac.tv.live.DecoderSession;
import ai.toastovac.tv.live.LiveHdrReceiver;

/**
 * Live 4K HDR HOME surface. Owns the one process HEVC decoder.
 * Overlay stays off. Do not start a second decoder.
 *
 * P2 safety: until the first decoder frame is presented, a status poster is
 * drawn on the SurfaceView so HOME never looks dead when the sender is down.
 */
public class LiveHdrActivity extends Activity implements SurfaceHolder.Callback {

    static final String TAG = "TOASTOVAC-LIVE";
    static final String DEFAULT_HOST = "192.168.1.155";
    static final int DEFAULT_PORT = 8768;
    static final int CONTROL_PORT = 8770;

    private SurfaceView sv;
    private LiveHdrReceiver rx;
    private BoxStatsReporter boxStats;
    private final Handler h = new Handler(Looper.getMainLooper());
    private boolean started;
    private boolean posterVisible;

    private final Paint bgPaint = new Paint();
    private final Paint textPaint = new Paint();
    private final Paint smallPaint = new Paint();

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        // P3.2: prefer the native-scene session's display mode. Select by
        // MEASURED mode properties (3840x2160 @ ~60.000), never by ordinal
        // index — the mode list differs across devices. This is a per-window
        // preference scoped to the live session, not a global change.
        requestTrue60DisplayMode();
        bgPaint.setColor(Color.rgb(8, 8, 12));
        textPaint.setColor(Color.rgb(200, 200, 215));
        textPaint.setTextSize(64f);
        textPaint.setAntiAlias(true);
        smallPaint.setColor(Color.rgb(120, 120, 140));
        smallPaint.setTextSize(34f);
        smallPaint.setAntiAlias(true);
        sv = new SurfaceView(this);
        sv.getHolder().addCallback(this);
        setContentView(sv, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT));
        DecoderSession.get().acquire(DecoderOwner.DASHBOARD);
        String host = DEFAULT_HOST;
        if (getIntent() != null && getIntent().getStringExtra("host") != null) {
            host = getIntent().getStringExtra("host");
        }
        rx = new LiveHdrReceiver(host, DEFAULT_PORT);
        boxStats = new BoxStatsReporter(host, CONTROL_PORT, rx);
    }

    /**
     * P3.2: request the measured 3840x2160 @ ~60.000 Hz display mode on this
     * window. Uses WindowManager.LayoutParams.preferredDisplayModeId (public
     * API). Mode is chosen by measured width/height/refreshRate, not by
     * ordinal index. On failure (no such mode) the display keeps its current
     * mode — the request is best-effort and non-destructive.
     */
    private void requestTrue60DisplayMode() {
        try {
            Display d = getDisplay();
            if (d == null) return;
            for (Display.Mode m : d.getSupportedModes()) {
                if (m.getPhysicalWidth() == 3840 && m.getPhysicalHeight() == 2160
                        && Math.abs(m.getRefreshRate() - 60.0f) < 0.05f) {
                    WindowManager.LayoutParams lp = getWindow().getAttributes();
                    lp.preferredDisplayModeId = m.getModeId();
                    getWindow().setAttributes(lp);
                    Log.i(TAG, "preferredDisplayModeId=" + m.getModeId()
                            + " refresh=" + m.getRefreshRate());
                    return;
                }
            }
            Log.w(TAG, "no true-60 4K mode exposed; keeping current display mode");
        } catch (Exception e) {
            Log.w(TAG, "preferredDisplayModeId failed", e);
        }
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        DecoderSession.get().ensureCreated(holder.getSurface());
        // P3.1: request 60.0 fps on the live video surface. FRAME_RATE_COMPATIBILITY_FIXED_SOURCE
        // tells the system this is fixed-rate video (not app UI), so it may switch the display
        // mode to the closest match (the measured 60.000 mode) instead of frame pacing tricks.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            try {
                Surface s = holder.getSurface();
                s.setFrameRate(60.0f, Surface.FRAME_RATE_COMPATIBILITY_FIXED_SOURCE,
                        Surface.CHANGE_FRAME_RATE_ALWAYS);
            } catch (Exception e) {
                Log.w(TAG, "setFrameRate failed", e);
            }
        }
        if (!started) {
            started = true;
            rx.start();
            boxStats.start();
        }
        h.post(drain);
        h.post(postPoster);
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        DecoderSession.get().ensureCreated(holder.getSurface());
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        // Keep decoder. Surface churn must not create decoder #2.
    }

    private final Runnable drain = new Runnable() {
        @Override public void run() {
            DecoderSession.get().drain();
            h.postDelayed(this, 8);
        }
    };

    /** Draw a safe status poster until the first decoder frame is presented. */
    private final Runnable postPoster = new Runnable() {
        @Override public void run() {
            DecoderSession dec = DecoderSession.get();
            if (dec.framesOut() > 0) {
                if (posterVisible) {
                    posterVisible = false;
                    h.removeCallbacks(this);
                }
                return;
            }
            posterVisible = true;
            Canvas c = null;
            try {
                c = sv.getHolder().lockCanvas();
                if (c != null) {
                    int w = c.getWidth();
                    int hgt = c.getHeight();
                    c.drawRect(0, 0, w, hgt, bgPaint);
                    String title = "Toastovač";
                    String sub = rx.status;
                    String decInfo = "decoder: " + dec.snapshot();
                    float cx = w / 2f;
                    c.drawText(title, cx - textPaint.measureText(title) / 2f, hgt * 0.42f, textPaint);
                    c.drawText(sub, cx - smallPaint.measureText(sub) / 2f, hgt * 0.52f, smallPaint);
                    c.drawText(decInfo, cx - smallPaint.measureText(decInfo) / 2f, hgt * 0.58f, smallPaint);
                }
            } catch (Exception e) {
                // surface busy with decoder: skip this poster frame
            } finally {
                if (c != null) sv.getHolder().unlockCanvasAndPost(c);
            }
            h.postDelayed(this, 500);
        }
    };

    @Override
    protected void onDestroy() {
        h.removeCallbacks(drain);
        h.removeCallbacks(postPoster);
        if (boxStats != null) boxStats.stop();
        if (rx != null) rx.stop();
        super.onDestroy();
    }
}