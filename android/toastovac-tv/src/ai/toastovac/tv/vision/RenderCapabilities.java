package ai.toastovac.tv.vision;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * Runtime report of which Vision Shell path is actually attached.
 * Dashboard, Overlay and MediaCodec video are independent planes.
 */
public final class RenderCapabilities {

    public boolean dashboardAttached;
    public int dashboardW;
    public int dashboardH;
    public boolean overlayAttached;
    public int overlayW;
    public int overlayH;
    public boolean overlayAlpha;
    public boolean mediaAttached;
    public int mediaW;
    public int mediaH;
    public String mediaPlane = "none";
    public String activeRole = "none";
    public String compositor = "";
    public String architecture = "hybrid-dashboard-4k-device-overlay-1080";

    // dashboard 4K codec pipeline
    public boolean pipelineRunning;
    public String pipelineReason = "init";
    public String encoderName = "none";
    public String decoderName = "none";
    public String codecMime = "none";
    public int encodedW;
    public int encodedH;
    public int decodedW;
    public int decodedH;
    public long framesSubmitted;
    public long framesDropped;
    public float renderMs;
    public float encLatencyMs;
    public float decLatencyMs;
    public float e2eMs;
    public int pipelineFps;
    public int gpuTargetW;
    public int gpuTargetH;

    public JSONObject toJson() {
        try {
            JSONObject o = new JSONObject();
            o.put("architecture", architecture);
            o.put("activeRole", activeRole);
            o.put("compositor", compositor);
            o.put("designW", RenderTarget.DESIGN_W);
            o.put("designH", RenderTarget.DESIGN_H);
            o.put("sharedSceneGraph", true);
            o.put("forkedUi", false);

            JSONObject dash = new JSONObject();
            dash.put("attached", dashboardAttached);
            dash.put("bufferW", dashboardW);
            dash.put("bufferH", dashboardH);
            dash.put("alpha", false);
            dash.put("rendererTarget", "3840x2160");
            dash.put("host", "DashboardVideoTarget HwcVideo DEVICE loop");
            o.put("dashboard", dash);

            JSONObject ov = new JSONObject();
            ov.put("attached", overlayAttached);
            ov.put("bufferW", overlayW);
            ov.put("bufferH", overlayH);
            ov.put("alpha", overlayAlpha);
            ov.put("rendererTarget", "1920x1080");
            ov.put("host", "View/onDraw TYPE_APPLICATION_OVERLAY");
            o.put("overlay", ov);

            JSONObject media = new JSONObject();
            media.put("attached", mediaAttached);
            media.put("bufferW", mediaW);
            media.put("bufferH", mediaH);
            media.put("plane", mediaPlane);
            o.put("media", media);

            JSONArray paths = new JSONArray();
            if (dashboardAttached) paths.put("DASHBOARD");
            if (overlayAttached) paths.put("OVERLAY");
            if (mediaAttached) paths.put("MEDIA");
            o.put("activePaths", paths);
            o.put("framesPainted", VisionRuntime.get().framesPainted);
            o.put("ticks", VisionRuntime.get().ticks);
            o.put("lastPaintError", VisionRuntime.get().lastPaintError);

            JSONObject pl = new JSONObject();
            pl.put("running", pipelineRunning);
            pl.put("reason", pipelineReason);
            pl.put("mime", codecMime);
            pl.put("encoder", encoderName);
            pl.put("decoder", decoderName);
            pl.put("gpuTargetW", gpuTargetW);
            pl.put("gpuTargetH", gpuTargetH);
            pl.put("encodedW", encodedW);
            pl.put("encodedH", encodedH);
            pl.put("decodedW", decodedW);
            pl.put("decodedH", decodedH);
            pl.put("framesSubmitted", framesSubmitted);
            pl.put("framesDropped", framesDropped);
            pl.put("renderMs", renderMs);
            pl.put("encLatencyMs", encLatencyMs);
            pl.put("decLatencyMs", decLatencyMs);
            pl.put("e2eMs", e2eMs);
            pl.put("fps", pipelineFps);
            o.put("pipeline", pl);
            return o;
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    public String toJsonString() {
        return toJson().toString();
    }
}
