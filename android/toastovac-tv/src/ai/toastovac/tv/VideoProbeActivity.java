package ai.toastovac.tv;

import android.app.Activity;
import android.media.MediaPlayer;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.WindowManager;
import android.widget.VideoView;

/**
 * VideoProbe — does a native (non-WebView) video surface reach the HwcVideo
 * plane at true 4K? Plays the 2160p source.mp4 via VideoView (MediaPlayer ->
 * MediaCodec -> video SurfaceView). Check dumpsys SurfaceFlinger for the
 * video layer: it must show an HwcVideo plane with a 3840x2160 display frame.
 */
public class VideoProbeActivity extends Activity {

    static final String TAG = "TOASTOVAC4K";
    static final String URL_SDR = "http://192.168.1.155:8766/video/stream/dQw4w9WgXcQ/source.mp4";
    static final String URL_HDR10 = "http://192.168.1.155:8766/video/stream/hdr10test/source.mp4";

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        VideoView vv = new VideoView(this);
        setContentView(vv);
        String clip = getIntent() != null ? getIntent().getStringExtra("clip") : null;
        String extra = getIntent() != null ? getIntent().getStringExtra("url") : null;
        String url;
        if (extra != null && !extra.isEmpty()) {
            url = extra;
        } else if ("hlg".equals(clip)) {
            url = resolveLocal("hlg.mp4");
        } else if ("hdr10".equals(clip)) {
            url = resolveLocal("hdr10.mp4");
        } else if ("live".equals(clip)) {
            url = resolveLocal("live_plain.mp4");
        } else if ("livehdr".equals(clip)) {
            url = resolveLocal("live_hdr.mp4");
        } else {
            Log.e(TAG, "refusing implicit SDR fallback");
            return;
        }
        if (url == null) {
            Log.e(TAG, "no playable source for clip=" + clip);
            return;
        }
        vv.setVideoURI(Uri.parse(url));
        vv.setOnPreparedListener(mp -> {
            Log.e(TAG, "VIDEO prepared: " + mp.getVideoWidth() + "x" + mp.getVideoHeight());
            mp.setLooping(true);
            vv.start();
        });
        vv.setOnErrorListener((mp, what, extraCode) -> {
            Log.e(TAG, "VIDEO error what=" + what + " extra=" + extraCode);
            return true;
        });
        Log.e(TAG, "VideoProbe loading " + url);
    }

    /**
     * Prefer world-readable /sdcard copy (same path that made HDR10 work).
     * Fall back to MediaStore, then APK asset extract. Never SDR Rickroll.
     */
    private String resolveLocal(String name) {
        java.io.File sd = new java.io.File("/sdcard/" + name);
        if (sd.isFile() && sd.length() > 1000) {
            android.net.Uri uri = queryMediaStore(name);
            if (uri != null) {
                Log.e(TAG, "using mediastore " + uri);
                return uri.toString();
            }
            Log.e(TAG, "using file " + sd);
            return Uri.fromFile(sd).toString();
        }
        String extracted = extractAsset(name);
        if (extracted != null) return extracted;
        return null;
    }

    private android.net.Uri queryMediaStore(String name) {
        try {
            android.database.Cursor c = getContentResolver().query(
                    android.provider.MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                    new String[]{android.provider.MediaStore.Video.Media._ID},
                    android.provider.MediaStore.Video.Media.DISPLAY_NAME + "=?",
                    new String[]{name}, null);
            if (c == null) return null;
            try {
                if (!c.moveToFirst()) return null;
                return android.content.ContentUris.withAppendedId(
                        android.provider.MediaStore.Video.Media.EXTERNAL_CONTENT_URI, c.getLong(0));
            } finally {
                c.close();
            }
        } catch (Exception e) {
            return null;
        }
    }

    private String extractAsset(String name) {
        java.io.File out = new java.io.File(getCacheDir(), name);
        try (java.io.InputStream in = getAssets().open(name);
             java.io.FileOutputStream fos = new java.io.FileOutputStream(out)) {
            byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) fos.write(buf, 0, n);
        } catch (Exception e) {
            Log.e(TAG, "extract " + name, e);
            return null;
        }
        if (!out.isFile() || out.length() < 1000) return null;
        Log.e(TAG, "extracted cache " + name + " " + out.length());
        return Uri.fromFile(out).toString();
    }
}