package ai.toastovac.tv;

import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

import ai.toastovac.tv.vision.SceneGraph;
import ai.toastovac.tv.vision.VisionRuntime;

/**
 * Polls the PC video server the same way the WebView Vision Shell did
 * (`/player/commands`). Commands drive the shared scene graph + native player.
 */
public final class CommandPoller {

    static final String TAG = "TOASTOVAC-CMD";
    static final int PERIOD_MS = 800;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final NativePlayer player;
    private volatile boolean running;
    private volatile boolean inFlight;
    private volatile String baseUrl = "http://192.168.1.155:8766";

    public CommandPoller(NativePlayer player) {
        this.player = player;
    }

    public void setBaseUrl(String url) {
        if (url == null) return;
        String u = url.trim();
        int q = u.indexOf('?');
        if (q > 0) u = u.substring(0, q);
        if (u.endsWith("/")) u = u.substring(0, u.length() - 1);
        baseUrl = u;
    }

    public String baseUrl() {
        return baseUrl;
    }

    public void start() {
        if (running) return;
        running = true;
        handler.post(tick);
    }

    public void stop() {
        running = false;
        handler.removeCallbacks(tick);
    }

    private final Runnable tick = new Runnable() {
        @Override         public void run() {
            if (!running) return;
            if (inFlight) {
                handler.postDelayed(this, PERIOD_MS);
                return;
            }
            inFlight = true;
            new Thread(() -> {
                try {
                    pollOnce();
                } catch (Exception e) {
                    Log.w(TAG, "poll: " + e.getMessage());
                } finally {
                    inFlight = false;
                    handler.postDelayed(CommandPoller.this.tick, PERIOD_MS);
                }
            }, "toastovac-cmd").start();
        }
    };

    private void pollOnce() throws Exception {
        String body = httpGet(baseUrl + "/player/commands");
        JSONObject o = new JSONObject(body);
        JSONArray cmds = o.optJSONArray("commands");
        if (cmds == null) return;
        for (int i = 0; i < cmds.length(); i++) {
            JSONObject c = cmds.getJSONObject(i);
            handler.post(() -> handle(c));
        }
    }

    void handle(JSONObject c) {
        String cmd = c.optString("cmd", "");
        JSONObject p = c.optJSONObject("payload");
        if (p == null) p = new JSONObject();
        VisionRuntime rt = VisionRuntime.get();
        rt.pulse();
        try {
            switch (cmd) {
                case "play": {
                    String id = p.optString("video_id", "");
                    if (!id.isEmpty()) playVideo(id);
                    break;
                }
                case "pause":
                    player.pause();
                    rt.graph.state = SceneGraph.ShellState.PAUSED;
                    break;
                case "stop":
                    player.pause();
                    player.seekSeconds(0);
                    rt.graph.state = SceneGraph.ShellState.PAUSED;
                    break;
                case "resume":
                    player.resume();
                    break;
                case "seek":
                    if (p.has("seconds")) player.seekSeconds((float) p.optDouble("seconds"));
                    break;
                case "volume":
                    if (p.has("level")) player.setVolume((float) p.optDouble("level"));
                    break;
                case "notify":
                    rt.toast(p.optString("text", ""));
                    break;
                case "state": {
                    String s = p.optString("state", "IDLE").toUpperCase();
                    SceneGraph.ShellState st;
                    try { st = SceneGraph.ShellState.valueOf(s); }
                    catch (Exception e) { st = SceneGraph.ShellState.IDLE; }
                    rt.graph.setVoice(st, p.optString("text", ""),
                            p.optString("status", ""),
                            android.os.SystemClock.uptimeMillis(),
                            p.optLong("hideAfter", 6000));
                    break;
                }
                default:
                    Log.i(TAG, "unhandled cmd " + cmd);
            }
        } catch (Exception e) {
            Log.e(TAG, "handle " + cmd, e);
        }
    }

    void playVideo(String videoId) {
        VisionRuntime rt = VisionRuntime.get();
        rt.graph.state = SceneGraph.ShellState.LOADING;
        rt.graph.status = "Načítám " + videoId + "…";
        new Thread(() -> {
            try {
                // Kick the download/transcode pipeline the same way the
                // WebView shell did, then read status for title + URI.
                try {
                    httpPost(baseUrl + "/video/command",
                            "{\"cmd\":\"play\",\"payload\":{\"video_id\":\"" + videoId + "\"}}");
                } catch (Exception ignored) {}
                String status = httpGet(baseUrl + "/video/status");
                JSONObject o = new JSONObject(status);
                JSONObject job = o.optJSONObject("job");
                JSONObject last = o.optJSONObject("last");
                JSONObject asset = job != null ? job.optJSONObject("asset") : last;
                String title = asset != null ? asset.optString("title", videoId) : videoId;
                // Prefer source.mp4 (true 4K DEVICE plane). Fall back to transcoded.
                String uri = baseUrl + "/video/stream/" + videoId + "/source.mp4";
                handler.post(() -> {
                    rt.graph.title = title;
                    rt.graph.status = title;
                    player.playUri(uri, title);
                });
            } catch (Exception e) {
                handler.post(() -> {
                    rt.graph.state = SceneGraph.ShellState.ERROR;
                    rt.toast("Play selhalo: " + e.getMessage());
                });
            }
        }, "toastovac-play").start();
    }

    static String httpPost(String url, String json) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(2500);
        c.setReadTimeout(4000);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        byte[] body = json.getBytes(StandardCharsets.UTF_8);
        c.getOutputStream().write(body);
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            return new String(bos.toByteArray(), StandardCharsets.UTF_8);
        } finally {
            c.disconnect();
        }
    }

    static String httpGet(String url) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(2500);
        c.setReadTimeout(4000);
        c.setRequestMethod("GET");
        try (InputStream in = c.getInputStream()) {
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            return new String(bos.toByteArray(), StandardCharsets.UTF_8);
        } finally {
            c.disconnect();
        }
    }
}
