package ai.toastovac.tv.vision;

/** Which physical host is painting the shared Vision Shell graph. */
public enum RenderRole {
    /** HOME launcher — native fullscreen Surface, 3840×2160. */
    DASHBOARD,
    /** TYPE_APPLICATION_OVERLAY companion — 1920×1080 RGBA. */
    OVERLAY
}
