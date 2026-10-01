package ai.toastovac.tv.live;

/** Only one HEVC decoder owner per process. */
public enum DecoderOwner {
    NONE,
    DASHBOARD,
    YOUTUBE,
    INTERVIEW,
    DESKTOP,
    MEDIA,
    PROBE
}
