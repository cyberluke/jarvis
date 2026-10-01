package ai.toastovac.tv.vision;

import android.os.SystemClock;
import android.util.Log;
import android.view.SurfaceHolder;

import java.io.File;
import java.io.FileWriter;
import java.util.EnumMap;

/**
 * One scene graph, one tick, N physical targets.
 * Dashboard and Overlay attach/detach independently.
 * Painting is owned by VisionView / CompanionOverlayService.
 */
public final class VisionRuntime {

    public static final String TAG = "TOASTOVAC-VISION";

    private static final VisionRuntime INSTANCE = new VisionRuntime();

    public static VisionRuntime get() {
        return INSTANCE;
    }

    public final SceneGraph graph = new SceneGraph();
    public final RenderCapabilities caps = new RenderCapabilities();

    private final EnumMap<RenderRole, Slot> slots = new EnumMap<>(RenderRole.class);

    private MediaHook media;
    private DashboardVideoTarget dashboardTarget;
    public volatile long framesPainted;
    public volatile long ticks;
    public volatile String lastPaintError = "";

    public interface MediaHook {
        boolean isPlaying();
        int videoWidth();
        int videoHeight();
        float position01();
        String title();
        void playUri(String uri, String title);
        void pause();
        void resume();
        void seekSeconds(float seconds);
        void setVolume(float level01);
    }

    private static final class Slot {
        final RenderTarget target;
        volatile SurfaceHolder holder;
        volatile boolean ready;

        Slot(RenderTarget target) {
            this.target = target;
        }
    }

    private VisionRuntime() {
        slots.put(RenderRole.DASHBOARD, new Slot(RenderTarget.dashboard()));
        slots.put(RenderRole.OVERLAY, new Slot(RenderTarget.overlay()));
    }

    public RenderTarget target(RenderRole role) {
        return slots.get(role).target;
    }

    public synchronized void attach(RenderRole role, SurfaceHolder holder) {
        Slot s = slots.get(role);
        s.holder = holder;
        s.ready = true;
        refreshCaps();
        Log.i(TAG, "attach " + role + " " + s.target.bufferW + "x" + s.target.bufferH);
    }

    /** Attach without a SurfaceHolder (View/onDraw host). */
    public void markAttached(RenderRole role) {
        attach(role, null);
    }

    public synchronized void detach(RenderRole role) {
        Slot s = slots.get(role);
        s.holder = null;
        s.ready = false;
        refreshCaps();
        Log.i(TAG, "detach " + role);
    }

    private File capsDump;

    public void setCapsDumpDir(File dir) {
        if (dir == null) return;
        try {
            dir.mkdirs();
            capsDump = new File(dir, "render_caps.json");
        } catch (Exception ignored) {
        }
    }

    public void setMediaHook(MediaHook hook) {
        this.media = hook;
        refreshCaps();
    }

    public void setDashboardTarget(DashboardVideoTarget target) {
        this.dashboardTarget = target;
        refreshCaps();
    }

    public MediaHook media() {
        return media;
    }

    public void pulse() {
        graph.pulse(SystemClock.uptimeMillis());
    }

    public void toast(String text) {
        graph.showToast(text, SystemClock.uptimeMillis(), 3000);
    }

    public synchronized String capabilitiesJson() {
        refreshCaps();
        return caps.toJsonString();
    }

    public void refreshCapsPublic() {
        refreshCaps();
    }

    public void syncMedia() {
        MediaHook m = media;
        if (m == null) {
            graph.videoMounted = false;
            return;
        }
        int w = m.videoWidth();
        graph.videoMounted = w > 0;
        graph.seek01 = m.position01();
        String title = m.title();
        if (title != null) graph.title = title;
        if (graph.videoMounted) {
            if (m.isPlaying() && graph.state != SceneGraph.ShellState.LISTENING
                    && graph.state != SceneGraph.ShellState.THINKING
                    && graph.state != SceneGraph.ShellState.SPEAKING
                    && graph.state != SceneGraph.ShellState.RESPONDING
                    && graph.state != SceneGraph.ShellState.ACTIVITY) {
                graph.state = SceneGraph.ShellState.PLAYING;
            }
        }
        caps.mediaAttached = w > 0;
        caps.mediaW = w;
        caps.mediaH = m.videoHeight();
        caps.mediaPlane = w > 0 ? "HwcVideo/MediaCodec" : "none";
    }

    private void refreshCaps() {
        Slot d = slots.get(RenderRole.DASHBOARD);
        Slot o = slots.get(RenderRole.OVERLAY);
        caps.dashboardAttached = d.ready;
        caps.dashboardW = d.target.bufferW;
        caps.dashboardH = d.target.bufferH;
        caps.overlayAttached = o.ready;
        caps.overlayW = o.target.bufferW;
        caps.overlayH = o.target.bufferH;
        caps.overlayAlpha = o.target.alpha;
        if (d.ready && o.ready) caps.activeRole = "DASHBOARD+OVERLAY";
        else if (d.ready) caps.activeRole = "DASHBOARD";
        else if (o.ready) caps.activeRole = "OVERLAY";
        else caps.activeRole = "none";
        caps.compositor = (d.ready ? d.target.compositorNote() : "")
                + (o.ready ? (d.ready ? " | " : "") + o.target.compositorNote() : "");
        MediaHook m = media;
        if (m != null) {
            caps.mediaAttached = m.videoWidth() > 0;
            caps.mediaW = m.videoWidth();
            caps.mediaH = m.videoHeight();
            caps.mediaPlane = caps.mediaAttached ? "HwcVideo/MediaCodec" : "none";
        }
        DashboardVideoTarget dv = dashboardTarget;
        if (dv != null) {
            caps.pipelineRunning = dv.pipelineRunning;
            caps.pipelineReason = dv.pipelineReason;
            caps.encoderName = dv.encoderName;
            caps.decoderName = dv.decoderName;
            caps.codecMime = dv.mime;
            caps.encodedW = dv.encodedW;
            caps.encodedH = dv.encodedH;
            caps.decodedW = dv.decodedW;
            caps.decodedH = dv.decodedH;
            caps.framesSubmitted = dv.framesSubmitted;
            caps.framesDropped = dv.framesDropped;
            caps.renderMs = dv.renderNs / 1_000_000f;
            caps.encLatencyMs = dv.encLatencyNs / 1_000_000f;
            caps.decLatencyMs = dv.decLatencyNs / 1_000_000f;
            caps.e2eMs = dv.e2eNs / 1_000_000f;
            caps.pipelineFps = dv.fps;
            caps.gpuTargetW = dv.encodeW;
            caps.gpuTargetH = dv.encodeH;
        }
        writeCapsDump();
    }

    private void writeCapsDump() {
        if (capsDump == null) return;
        try (FileWriter w = new FileWriter(capsDump, false)) {
            w.write(caps.toJsonString());
        } catch (Exception ignored) {
        }
    }

    /** Snapshot for adb/JS. Always recomputes from current slots. */
    public String snapshot() {
        refreshCaps();
        return caps.toJsonString();
    }
}
