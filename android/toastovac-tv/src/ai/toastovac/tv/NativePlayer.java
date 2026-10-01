package ai.toastovac.tv;

import android.content.Context;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.net.Uri;
import android.util.Log;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.ViewGroup;
import android.widget.FrameLayout;

import ai.toastovac.tv.vision.VisionRuntime;

/**
 * Native 4K MediaCodec path (VideoView-equivalent). Stays on the HwcVideo
 * DEVICE plane. The Vision Shell UI is a separate Surface on top.
 */
public final class NativePlayer implements VisionRuntime.MediaHook {

    static final String TAG = "TOASTOVAC-MEDIA";

    private final Context ctx;
    private final SurfaceView surface;
    private MediaPlayer player;
    private String title = "";
    private int vw;
    private int vh;
    private boolean prepared;
    private boolean wantPlay;

    public NativePlayer(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        surface = new SurfaceView(ctx);
        surface.getHolder().addCallback(new SurfaceHolder.Callback() {
            @Override public void surfaceCreated(SurfaceHolder h) {
                bindSurface();
            }
            @Override public void surfaceChanged(SurfaceHolder h, int f, int w, int hh) {}
            @Override public void surfaceDestroyed(SurfaceHolder h) {
                if (player != null) {
                    try { player.setDisplay(null); } catch (Exception ignored) {}
                }
            }
        });
    }

    public SurfaceView view() {
        return surface;
    }

    public void attachTo(FrameLayout parent) {
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT);
        parent.addView(surface, 0, lp);
    }

    public void release() {
        if (player != null) {
            try { player.release(); } catch (Exception ignored) {}
            player = null;
        }
        prepared = false;
        vw = vh = 0;
    }

    @Override
    public void playUri(String uri, String newTitle) {
        title = newTitle == null ? "" : newTitle;
        wantPlay = true;
        release();
        try {
            player = new MediaPlayer();
            player.setAudioStreamType(AudioManager.STREAM_MUSIC);
            player.setDataSource(ctx, Uri.parse(uri));
            player.setOnPreparedListener(mp -> {
                vw = mp.getVideoWidth();
                vh = mp.getVideoHeight();
                prepared = true;
                mp.setLooping(true);
                bindSurface();
                if (wantPlay) mp.start();
                Log.i(TAG, "prepared " + vw + "x" + vh + " " + uri);
            });
            player.setOnErrorListener((mp, what, extra) -> {
                Log.e(TAG, "error what=" + what + " extra=" + extra);
                VisionRuntime.get().graph.state = ai.toastovac.tv.vision.SceneGraph.ShellState.ERROR;
                VisionRuntime.get().toast("Chyba přehrávání " + what);
                return true;
            });
            player.setOnVideoSizeChangedListener((mp, w, h) -> {
                vw = w;
                vh = h;
            });
            player.prepareAsync();
        } catch (Exception e) {
            Log.e(TAG, "playUri failed", e);
            VisionRuntime.get().toast("Nelze otevřít video");
        }
    }

    private void bindSurface() {
        if (player != null && surface.getHolder().getSurface() != null
                && surface.getHolder().getSurface().isValid()) {
            try { player.setDisplay(surface.getHolder()); } catch (Exception ignored) {}
        }
    }

    @Override public boolean isPlaying() {
        try { return player != null && prepared && player.isPlaying(); }
        catch (Exception e) { return false; }
    }

    @Override public int videoWidth() { return vw; }
    @Override public int videoHeight() { return vh; }

    @Override
    public float position01() {
        try {
            if (player == null || !prepared) return 0f;
            int d = player.getDuration();
            if (d <= 0) return 0f;
            return player.getCurrentPosition() / (float) d;
        } catch (Exception e) {
            return 0f;
        }
    }

    @Override public String title() { return title; }

    @Override
    public void pause() {
        wantPlay = false;
        try { if (player != null && prepared) player.pause(); } catch (Exception ignored) {}
    }

    @Override
    public void resume() {
        wantPlay = true;
        try { if (player != null && prepared) player.start(); } catch (Exception ignored) {}
    }

    @Override
    public void seekSeconds(float seconds) {
        try {
            if (player != null && prepared) {
                player.seekTo(Math.max(0, (int) (seconds * 1000)));
            }
        } catch (Exception ignored) {}
    }

    @Override
    public void setVolume(float level01) {
        float v = Math.max(0f, Math.min(1f, level01));
        try { if (player != null) player.setVolume(v, v); } catch (Exception ignored) {}
    }
}
