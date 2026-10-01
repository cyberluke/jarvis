package ai.toastovac.tv;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.PixelFormat;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.KeyEvent;
import android.view.SurfaceView;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.FrameLayout;

import ai.toastovac.tv.vision.DashboardVideoTarget;
import ai.toastovac.tv.vision.VisionRuntime;

/**
 * Toastovač HOME — TRUE 4K native dashboard.
 *
 * Layers (bottom → top):
 *   NativePlayer SurfaceView        — user video (HwcVideo DEVICE plane)
 *   DashboardVideoTarget SurfaceView — SceneGraph → HW encode → HW decode →
 *                                      HwcVideo DEVICE plane at 3840×2160
 *
 * The dashboard is opaque and fullscreen, so the encoder/decoder loop is the
 * correct path: the only proven way to get visible true-4K pixels on this
 * panel. No CLIENT UI on the HOME path.
 */
public class MainActivity extends Activity {

    static final String PREFS = "toastovac";
    static final String KEY_URL = "server_url";
    static final String DEFAULT_URL = "http://192.168.1.155:8766/?tv=1";

    private NativePlayer player;
    private CommandPoller poller;
    private DashboardVideoTarget dvt;
    private final Handler handler = new Handler(Looper.getMainLooper());

    private final Runnable modePump = new Runnable() {
        @Override public void run() {
            if (dvt != null) dvt.updateMode();
            handler.postDelayed(this, 500);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        VisionRuntime rt = VisionRuntime.get();
        java.io.File files = getExternalFilesDir(null);
        rt.setCapsDumpDir(files);
        ai.toastovac.tv.vision.PipelineLog.init(files);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0xFF030305);

        // bottom: user media
        player = new NativePlayer(this);
        player.attachTo(root);
        rt.setMediaHook(player);

        // top: true-4K dashboard UI through the codec loop
        SurfaceView uiOut = new SurfaceView(this);
        uiOut.setZOrderMediaOverlay(false);
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT);
        root.addView(uiOut, lp);

        dvt = new DashboardVideoTarget(uiOut);
        if (files != null) dvt.setStreamFile(new java.io.File(files, "dash4k.h265"));
        rt.setDashboardTarget(dvt);

        setContentView(root);
        hideSystemBars();

        poller = new CommandPoller(player);
        poller.setBaseUrl(dashboardUrl());
        poller.start();

        rt.graph.brand = "Toastovač";
        rt.graph.status = "4K · DEVICE loop";
        rt.graph.hint = "Řekni Toastovači, co má hrát";

        handler.post(modePump);
    }

    String dashboardUrl() {
        SharedPreferences p = getSharedPreferences(PREFS, MODE_PRIVATE);
        return p.getString(KEY_URL, DEFAULT_URL);
    }

    void setServerUrl(String url) {
        if (url == null || url.trim().isEmpty()) return;
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_URL, url.trim()).apply();
        if (poller != null) poller.setBaseUrl(url);
    }

    public NativePlayer player() {
        return player;
    }

    public CommandPoller poller() {
        return poller;
    }

    private void hideSystemBars() {
        if (Build.VERSION.SDK_INT >= 30) {
            Window w = getWindow();
            w.setDecorFitsSystemWindows(false);
            WindowInsetsController c = w.getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        }
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent event) {
        if (event.getAction() != KeyEvent.ACTION_DOWN) {
            return super.dispatchKeyEvent(event);
        }
        VisionRuntime rt = VisionRuntime.get();
        switch (event.getKeyCode()) {
            case KeyEvent.KEYCODE_BACK:
                rt.pulse();
                if (player != null && player.isPlaying()) {
                    player.pause();
                    rt.graph.state = ai.toastovac.tv.vision.SceneGraph.ShellState.PAUSED;
                }
                return true;
            case KeyEvent.KEYCODE_DPAD_CENTER:
            case KeyEvent.KEYCODE_ENTER:
                rt.pulse();
                if (player != null) {
                    if (player.isPlaying()) player.pause();
                    else player.resume();
                }
                return true;
            case KeyEvent.KEYCODE_MENU:
                // Overlay auto-start frozen during HDMI/HDR investigation.
                // Rescue scripts re-enable later. Do not start CompanionOverlayService.
                return true;
            default:
                return super.dispatchKeyEvent(event);
        }
    }

    void toggleOverlay() {
        if (Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
            VisionRuntime.get().toast("Povol overlay: appops SYSTEM_ALERT_WINDOW");
            return;
        }
        startService(new Intent(this, CompanionOverlayService.class));
        VisionRuntime.get().toast("Companion overlay");
    }

    @Override
    protected void onResume() {
        super.onResume();
        hideSystemBars();
        if (dvt != null) dvt.start();
    }

    @Override
    protected void onPause() {
        // Keep the 4K decoder alive across transient pauses (HOME bounce).
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(modePump);
        if (poller != null) poller.stop();
        // Do not stop the 4K decoder from onDestroy of a bouncing
        // ActivityRecord — BLAST recreates MainActivity and a second
        // decoder configure panics the kernel. Process death releases it.
        VisionRuntime.get().setMediaHook(null);
        if (player != null) player.release();
        super.onDestroy();
    }
}