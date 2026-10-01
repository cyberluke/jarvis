package ai.toastovac.tv.live;

import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Process;
import android.os.SystemClock;
import android.util.Log;
import android.view.Surface;

import java.nio.ByteBuffer;

/**
 * Process-singleton HEVC decoder. Creating a second
 * c2.amlogic.hevc.decoder instance kernel-panics this SoC.
 */
public final class DecoderSession {

    public static final String TAG = "TOASTOVAC-DEC";
    public static final String CODEC = "c2.amlogic.hevc.decoder";
    public static final String MIME = "video/hevc";
    public static final int W = 3840;
    public static final int H = 2160;

    public enum State {
        STOPPED, STARTING, WAITING_FOR_CONFIG, WAITING_FOR_IDR,
        RUNNING, STARVED, RECOVERING, FAILED
    }

    private static final DecoderSession INSTANCE = new DecoderSession();

    public static DecoderSession get() {
        return INSTANCE;
    }

    private MediaCodec codec;
    private Surface surface;
    private DecoderOwner owner = DecoderOwner.NONE;
    private State state = State.STOPPED;
    private int createCount;
    private long createdAtMs;
    private int createdPid;
    private String lastError = "";
    private long framesIn;
    private long framesOut;
    private long framesDropped;
    private long inputFullEvents;   // dequeueInputBuffer returned -1 (input full)
    private long inputFullDropped;  // AU dropped after the retry budget (never expected)
    private long lastVideoPtsUs;
    private long audioEpochAtStreamStart = -1;
    private volatile boolean configured;

    private DecoderSession() {}

    public synchronized State state() { return state; }
    public synchronized int createCount() { return createCount; }
    public synchronized DecoderOwner owner() { return owner; }
    public synchronized String lastError() { return lastError; }
    public synchronized long framesIn() { return framesIn; }
    public synchronized long framesOut() { return framesOut; }
    public synchronized long framesDropped() { return framesDropped; }
    public synchronized long inputFullEvents() { return inputFullEvents; }
    public synchronized long inputFullDropped() { return inputFullDropped; }
    public synchronized long lastVideoPtsUs() { return lastVideoPtsUs; }
    public synchronized boolean configured() { return configured; }

    public synchronized boolean acquire(DecoderOwner who) {
        if (owner != DecoderOwner.NONE && owner != who) {
            Log.e(TAG, "acquire denied have=" + owner + " want=" + who);
            return false;
        }
        owner = who;
        return true;
    }

    /**
     * Create the decoder at most once per process. Surface may be bound later
     * only if we have not yet configured — after configure, Surface churn
     * must not create decoder #2.
     */
    public synchronized boolean ensureCreated(Surface out) {
        if (codec != null) {
            if (out != null && out.isValid()) surface = out;
            return true;
        }
        if (createCount > 0) {
            lastError = "refusing decoder #2 createCount=" + createCount;
            Log.e(TAG, lastError);
            state = State.FAILED;
            return false;
        }
        if (out == null || !out.isValid()) {
            lastError = "no surface";
            return false;
        }
        state = State.STARTING;
        try {
            MediaFormat fmt = MediaFormat.createVideoFormat(MIME, W, H);
            fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 2 * 1024 * 1024);
            // Amlogic C2 decoders apply a hardware transform derived from the
            // rotation key in the input format; MediaPlayer always passes
            // rotation=0, and when the key is absent some builds default to a
            // 180° flip. Force 0 to match the golden-clip path.
            fmt.setInteger(MediaFormat.KEY_ROTATION, 0);
            // Do not set COLOR_STANDARD / TRANSFER here — let bitstream VUI
            // drive BT2020_ITU_PQ the same way the golden HDR10 clip did.
            codec = MediaCodec.createByCodecName(CODEC);
            codec.configure(fmt, out, null, 0);
            codec.start();
            surface = out;
            configured = true;
            createCount = 1;
            createdAtMs = SystemClock.uptimeMillis();
            createdPid = Process.myPid();
            state = State.WAITING_FOR_CONFIG;
            Log.e(TAG, "CREATED pid=" + createdPid + " t=" + createdAtMs
                    + " owner=" + owner + " count=" + createCount);
            return true;
        } catch (Exception e) {
            lastError = String.valueOf(e);
            Log.e(TAG, "create failed", e);
            releaseQuiet();
            state = State.FAILED;
            return false;
        }
    }

    public synchronized boolean queue(byte[] annexb, int flags, long ptsUs) {
        if (codec == null) return false;
        try {
            // Never silently drop: if the input queue is momentarily full
            // (decoder still draining), wait for a buffer instead of losing
            // the frame. A dropped AU reads as a visible blink/jump.
            // Retry budget ~120 ms (previous code dropped after a single
            // 20 ms timeout — the measured framesIn-framesOut gap growth).
            int idx = -1;
            long waitUntil = SystemClock.uptimeMillis() + 120;
            while (idx < 0 && SystemClock.uptimeMillis() < waitUntil) {
                idx = codec.dequeueInputBuffer(20_000);
                if (idx < 0) {
                    inputFullEvents++;
                    drain(); // free output buffers so input can drain
                    Thread.sleep(2);
                }
            }
            if (idx < 0) {
                inputFullDropped++;
                lastError = "input full, dropped AU";
                Log.w(TAG, "input full, AU dropped (in=" + framesIn + ")");
                return false;
            }
            ByteBuffer in = codec.getInputBuffer(idx);
            in.clear();
            int n = Math.min(in.remaining(), annexb.length);
            in.put(annexb, 0, n);
            int mcFlags = 0;
            if ((flags & LiveProtocol.FLAG_CONFIG) != 0) {
                mcFlags |= MediaCodec.BUFFER_FLAG_CODEC_CONFIG;
                // A new video stream starts here (source switch): the audio
                // clock from the previous stream is stale until the audio
                // (re)configures. Record the epoch so the too-late drop gate
                // only applies once the audio clock is coherent with THIS
                // stream (fixes the transition drop storm: video pts reset to
                // 0 while the old audio clock was minutes ahead → every frame
                // read "too late" → dropped).
                audioEpochAtStreamStart = AudioSession.get().configureEpoch();
            }
            if ((flags & LiveProtocol.FLAG_IDR) != 0) {
                mcFlags |= MediaCodec.BUFFER_FLAG_KEY_FRAME;
            }
            codec.queueInputBuffer(idx, 0, n, ptsUs, mcFlags);
            framesIn++;
            if ((flags & LiveProtocol.FLAG_IDR) != 0) {
                if (state == State.WAITING_FOR_CONFIG || state == State.WAITING_FOR_IDR) {
                    Log.i(TAG, "FIRST_IDR bytes=" + n + " flags=0x" + Integer.toHexString(flags));
                }
                state = State.RUNNING;
            } else if (state == State.STOPPED || state == State.STARTING) {
                state = State.WAITING_FOR_IDR;
            }
            return true;
        } catch (Exception e) {
            lastError = String.valueOf(e);
            Log.e(TAG, "queue", e);
            return false;
        }
    }

    public synchronized void drain() {
        if (codec == null) return;
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        try {
            int idx;
            while ((idx = codec.dequeueOutputBuffer(info, 0)) >= 0) {
                lastVideoPtsUs = info.presentationTimeUs;
                AudioSession aud = AudioSession.get();
                long clock = aud.audioClockUs();
                boolean render = true;
                // Drop video only when the audio clock is coherent with the
                // CURRENT stream: the audio must have (re)configured AFTER the
                // video stream started. Audio-less sources (native scene,
                // interview) keep a stale clock from the previous source —
                // gating on it would drop the whole new stream as "too late".
                boolean audioCoherent = aud.configureEpoch() > audioEpochAtStreamStart;
                if (audioCoherent && clock >= 0 && info.presentationTimeUs > 0) {
                    long deltaMs = (info.presentationTimeUs - clock) / 1000L;
                    if (deltaMs < -200) {
                        render = false;
                        framesDropped++;
                    }
                    aud.noteVideoPts(info.presentationTimeUs);
                }
                codec.releaseOutputBuffer(idx, render);
                if (render) framesOut++;
            }
            if (idx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                MediaFormat f = codec.getOutputFormat();
                Log.e(TAG, "out fmt " + f);
            }
        } catch (Exception e) {
            lastError = String.valueOf(e);
        }
    }

    public synchronized String snapshot() {
        return "owner=" + owner + " state=" + state + " creates=" + createCount
                + " in=" + framesIn + " out=" + framesOut
                + " drop=" + framesDropped
                + " inputFull=" + inputFullEvents + "/" + inputFullDropped
                + " vpts=" + lastVideoPtsUs
                + " err=" + lastError
                + " pid=" + createdPid;
    }

    private void releaseQuiet() {
        if (codec != null) {
            try { codec.stop(); } catch (Exception ignored) {}
            try { codec.release(); } catch (Exception ignored) {}
            codec = null;
        }
        configured = false;
    }
}
