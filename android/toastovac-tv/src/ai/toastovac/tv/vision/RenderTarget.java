package ai.toastovac.tv.vision;

/**
 * Physical paint target. Dashboard and Overlay share one scene graph;
 * only this object differs.
 *
 * Design space is always 1920×1080. Physical pixels = design × scale.
 */
public final class RenderTarget {

    public static final int DESIGN_W = 1920;
    public static final int DESIGN_H = 1080;

    public final RenderRole role;
    public final int bufferW;
    public final int bufferH;
    public final boolean alpha;
    public final boolean opaqueBackground;
    public final float scaleX;
    public final float scaleY;

    private RenderTarget(RenderRole role, int bufferW, int bufferH,
                         boolean alpha, boolean opaqueBackground) {
        this.role = role;
        this.bufferW = bufferW;
        this.bufferH = bufferH;
        this.alpha = alpha;
        this.opaqueBackground = opaqueBackground;
        this.scaleX = bufferW / (float) DESIGN_W;
        this.scaleY = bufferH / (float) DESIGN_H;
    }

    /** True 4K dashboard buffer. Do not use for overlay. */
    public static RenderTarget dashboard() {
        return new RenderTarget(RenderRole.DASHBOARD, 3840, 2160, false, true);
    }

    /**
     * Companion overlay. Paint 1080p — Meson CLIENT composition downsamples
     * any 4K RGBA overlay anyway, so 4K here is wasted Mali work.
     */
    public static RenderTarget overlay() {
        return new RenderTarget(RenderRole.OVERLAY, 1920, 1080, true, false);
    }

    public RenderTarget withBuffer(int w, int h) {
        return new RenderTarget(role, w, h, alpha, opaqueBackground);
    }

    public String compositorNote() {
        if (role == RenderRole.DASHBOARD) {
            return "native-surface-3840x2160; SF CLIENT may still composite at 1080p";
        }
        return "overlay-rgba-1920x1080; accepted CLIENT 1080p path";
    }
}
