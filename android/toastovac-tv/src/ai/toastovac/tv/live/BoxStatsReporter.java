package ai.toastovac.tv.live;

import android.os.SystemClock;
import android.util.Log;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Box -> host telemetry (P1.5 / P3.3). Periodically POSTs decoder + receiver
 * state to the cast host's /cast/boxstats endpoint so the host can expose the
 * measured boxOutputFps, decoder createCount, drops, and state — never the
 * configured FPS. The host is unreachable during a restart; the loop simply
 * retries next tick.
 */
public final class BoxStatsReporter {

    static final String TAG = "TOASTOVAC-BOXSTATS";
    static final int PERIOD_MS = 3000;

    private final String host;
    private final int port;
    private final LiveHdrReceiver rx;
    private volatile boolean running;
    private Thread thread;
    private long lastOut;
    private long lastTs;

    public BoxStatsReporter(String host, int port, LiveHdrReceiver rx) {
        this.host = host;
        this.port = port;
        this.rx = rx;
    }

    public void start() {
        if (running) return;
        running = true;
        thread = new Thread(this::loop, "box-stats");
        thread.start();
    }

    public void stop() {
        running = false;
        if (thread != null) thread.interrupt();
    }

    private void loop() {
        while (running) {
            try {
                Thread.sleep(PERIOD_MS);
            } catch (InterruptedException e) {
                break;
            }
            try {
                post();
            } catch (Exception e) {
                // host down/restarting: retry next tick, never fatal
            }
        }
    }

    private void post() throws Exception {
        DecoderSession dec = DecoderSession.get();
        long now = SystemClock.uptimeMillis();
        long out = dec.framesOut();
        double outputFps = 0.0;
        if (lastTs > 0) {
            double dt = (now - lastTs) / 1000.0;
            if (dt > 0) outputFps = (out - lastOut) / dt;
        }
        lastOut = out;
        lastTs = now;

        JSONObject o = new JSONObject();
        o.put("createCount", dec.createCount());
        o.put("state", String.valueOf(dec.state()));
        o.put("owner", String.valueOf(dec.owner()));
        o.put("framesIn", dec.framesIn());
        o.put("framesOut", dec.framesOut());
        o.put("framesDropped", dec.framesDropped());
        o.put("inputFullEvents", dec.inputFullEvents());
        o.put("inputFullDropped", dec.inputFullDropped());
        o.put("lastVideoPtsUs", dec.lastVideoPtsUs());
        o.put("lastError", dec.lastError());
        o.put("outputFps", Math.round(outputFps * 100.0) / 100.0);
        o.put("rxPackets", rx.packets);
        o.put("rxVideoPackets", rx.videoPackets);
        o.put("rxStatus", rx.status);
        o.put("upMs", now);

        byte[] body = o.toString().getBytes(StandardCharsets.UTF_8);
        HttpURLConnection c = (HttpURLConnection)
                new URL("http://" + host + ":" + port + "/cast/boxstats").openConnection();
        c.setConnectTimeout(2000);
        c.setReadTimeout(2000);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        c.setFixedLengthStreamingMode(body.length);
        try (OutputStream os = c.getOutputStream()) {
            os.write(body);
        }
        int code = c.getResponseCode();
        c.disconnect();
        if (code != 200) Log.w(TAG, "post rc=" + code);
    }
}