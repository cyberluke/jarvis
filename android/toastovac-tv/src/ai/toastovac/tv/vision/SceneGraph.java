package ai.toastovac.tv.vision;

/**
 * Shared Vision Shell scene. One instance, two physical targets.
 *
 * Logical layout is 1920×1080. Nodes do not know buffer size.
 */
public final class SceneGraph {

    public enum ShellState {
        IDLE, LOADING, PLAYING, PAUSED, ERROR,
        LISTENING, THINKING, SPEAKING, RESPONDING, ACTIVITY
    }

    public volatile ShellState state = ShellState.IDLE;
    public volatile String brand = "Toastovač";
    public volatile String status = "Waiting…";
    public volatile String hint = "Řekni Toastovači, co má hrát";
    public volatile String voiceCaption = "";
    public volatile String voiceStatus = "";
    public volatile String toast = "";
    public volatile String title = "";
    public volatile String interviewMode = "";
    public volatile String interviewQuestion = "";
    public volatile String interviewCoach = "";
    public volatile String interviewPhase = "";
    public volatile String interviewProgress = "";
    public volatile boolean videoMounted = false;
    public volatile float seek01 = 0f;
    public volatile long toastUntilMs;
    public volatile long activityUntilMs;
    public volatile long respondUntilMs;

    /** 0..1 spark travel along the bottom line. */
    public float sparkPhase;

    public void tick(float dt, long nowMs) {
        sparkPhase += dt * 0.35f;
        if (sparkPhase > 1f) sparkPhase -= 1f;
        if (toastUntilMs != 0 && nowMs > toastUntilMs) {
            toast = "";
            toastUntilMs = 0;
        }
        if (state == ShellState.ACTIVITY && nowMs > activityUntilMs) {
            state = videoMounted ? (seek01 > 0 ? ShellState.PLAYING : ShellState.PAUSED)
                    : ShellState.IDLE;
        }
        if (state == ShellState.RESPONDING && nowMs > respondUntilMs) {
            state = videoMounted ? ShellState.PLAYING : ShellState.IDLE;
            voiceCaption = "";
            voiceStatus = "";
        }
    }

    public void pulse(long nowMs) {
        if (state == ShellState.LISTENING || state == ShellState.THINKING
                || state == ShellState.SPEAKING || state == ShellState.RESPONDING) {
            return;
        }
        state = ShellState.ACTIVITY;
        activityUntilMs = nowMs + 1200;
    }

    public void showToast(String text, long nowMs, long holdMs) {
        toast = text == null ? "" : text;
        toastUntilMs = nowMs + holdMs;
    }

    public void setVoice(ShellState s, String caption, String statusLine, long nowMs, long holdMs) {
        state = s;
        voiceCaption = caption == null ? "" : caption;
        voiceStatus = statusLine == null ? "" : statusLine;
        if (s == ShellState.RESPONDING) respondUntilMs = nowMs + holdMs;
    }

    public void setInterview(String mode, String question, String coach,
                             String phase, String progress) {
        interviewMode = mode == null ? "" : mode;
        interviewQuestion = question == null ? "" : question;
        interviewCoach = coach == null ? "" : coach;
        interviewPhase = phase == null ? "" : phase;
        interviewProgress = progress == null ? "" : progress;
    }

    public boolean veil() {
        return state == ShellState.LISTENING
                || state == ShellState.THINKING
                || state == ShellState.RESPONDING;
    }
}
