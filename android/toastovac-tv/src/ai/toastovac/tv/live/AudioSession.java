package ai.toastovac.tv.live;

import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.SystemClock;
import android.util.Log;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;

/**
 * Persistent PCM sink. One AudioTrack for the process.
 * Does not touch the HEVC decoder.
 */
public final class AudioSession {

    public static final String TAG = "TOASTOVAC-AUD";
    public static final int DEFAULT_RATE = 48000;
    public static final int DEFAULT_CH = 2;
    public static final int BYTES_PER_SAMPLE = 2;

    private static final AudioSession INSTANCE = new AudioSession();

    public static AudioSession get() {
        return INSTANCE;
    }

    private AudioTrack track;
    private int sampleRate = DEFAULT_RATE;
    private int channels = DEFAULT_CH;
    private long bytesQueued;
    private long underruns;
    private long packets;
    private long lastPtsUs;
    private long originElapsedUs = -1;
    private long originPtsUs;
    private final ArrayList<Integer> deltas = new ArrayList<>();
    private volatile String lastError = "";
    private volatile boolean configured;
    private long configureEpoch;

    private AudioSession() {}

    public synchronized boolean configured() { return configured; }
    public synchronized long packets() { return packets; }
    public synchronized long bytesQueued() { return bytesQueued; }
    public synchronized long underruns() { return underruns; }
    public synchronized long lastPtsUs() { return lastPtsUs; }
    public synchronized String lastError() { return lastError; }

    /** Monotonic count of configure() calls — lets the decoder know whether
     *  the audio clock was re-anchored AFTER the current video stream started
     *  (source switches; without audio, the stale clock must not gate video). */
    public synchronized long configureEpoch() { return configureEpoch; }

    /** Playback clock in media microseconds, derived from AudioTrack head. */
    public synchronized long audioClockUs() {
        if (track == null) return -1;
        int head = track.getPlaybackHeadPosition();
        if (head < 0) return -1;
        long frames = head & 0xffffffffL;
        return originPtsUs + (frames * 1_000_000L) / Math.max(1, sampleRate);
    }

    public synchronized int playbackHead() {
        return track == null ? -1 : track.getPlaybackHeadPosition();
    }

    public synchronized void configure(byte[] cfg) {
        if (cfg == null || cfg.length < 12) {
            lastError = "short audio config";
            return;
        }
        ByteBuffer bb = ByteBuffer.wrap(cfg).order(ByteOrder.LITTLE_ENDIAN);
        int rate = bb.getInt();
        int ch = bb.getShort() & 0xFFFF;
        int bits = bb.getShort() & 0xFFFF;
        if (rate <= 0 || ch < 1 || ch > 2 || bits != 16) {
            lastError = "bad audio cfg rate=" + rate + " ch=" + ch + " bits=" + bits;
            Log.e(TAG, lastError);
            return;
        }
        if (track != null && sampleRate == rate && channels == ch) {
            // Same format on an existing track = a NEW stream (source switch /
            // re-cast). The video timeline restarts at 0 with the new producer,
            // so the audio clock must re-anchor to the new stream too.
            // Keeping the cumulative process-lifetime clock makes every new
            // frame read as "too late" (deltaMs < -200) → black video until
            // the app is restarted. Flush discards the stale stream's PCM and
            // resets the playback head baseline to 0.
            try { track.pause(); } catch (Throwable ignored) {}
            try { track.flush(); } catch (Throwable ignored) {}
            try { track.play(); } catch (Throwable ignored) {}
            originElapsedUs = -1;
            originPtsUs = 0;
            bytesQueued = 0;
            deltas.clear();
            configured = true;
            configureEpoch++;
            Log.i(TAG, "RE_CONFIG rate=" + rate + " ch=" + ch
                    + " (new stream, audio clock re-anchored)");
            return;
        }
        releaseQuiet();
        sampleRate = rate;
        channels = ch;
        int chMask = ch == 1
                ? AudioFormat.CHANNEL_OUT_MONO
                : AudioFormat.CHANNEL_OUT_STEREO;
        int min = AudioTrack.getMinBufferSize(rate, chMask, AudioFormat.ENCODING_PCM_16BIT);
        // ~80 ms of PCM: enough to absorb LAN jitter, small enough for A/V
        int want = rate * ch * BYTES_PER_SAMPLE / 12;
        int buf = Math.max(min, want);
        track = new AudioTrack(
                AudioManager.STREAM_MUSIC,
                rate, chMask, AudioFormat.ENCODING_PCM_16BIT,
                buf, AudioTrack.MODE_STREAM);
        track.play();
        configured = true;
        originElapsedUs = -1;
        originPtsUs = 0;
        bytesQueued = 0;
        packets = 0;
        configureEpoch++;
        Log.i(TAG, "CONFIG rate=" + rate + " ch=" + ch + " buf=" + buf);
    }

    public synchronized void queue(byte[] pcm, long ptsUs) {
        if (track == null) {
            configure(defaultConfig());
        }
        if (track == null || pcm == null || pcm.length == 0) return;
        if (originElapsedUs < 0) {
            originElapsedUs = SystemClock.elapsedRealtime() * 1000L;
            originPtsUs = ptsUs;
        }
        int wrote = track.write(pcm, 0, pcm.length);
        if (wrote < 0) {
            lastError = "write " + wrote;
            return;
        }
        bytesQueued += wrote;
        packets++;
        lastPtsUs = ptsUs;
        try {
            int u = track.getUnderrunCount();
            if (u > underruns) underruns = u;
        } catch (Throwable ignored) {}
    }

    /** Record video-vs-audio offset in milliseconds. */
    public synchronized void noteVideoPts(long videoPtsUs) {
        long clock = audioClockUs();
        if (clock < 0 || videoPtsUs <= 0) return;
        int d = (int) ((videoPtsUs - clock) / 1000L);
        if (deltas.size() < 4000) deltas.add(d);
    }

    public synchronized int[] deltaStats() {
        if (deltas.isEmpty()) return new int[]{0, 0, 0, 0, 0};
        ArrayList<Integer> copy = new ArrayList<>(deltas);
        java.util.Collections.sort(copy);
        long sum = 0;
        int maxAbs = 0;
        for (int v : copy) {
            sum += v;
            int a = v < 0 ? -v : v;
            if (a > maxAbs) maxAbs = a;
        }
        int n = copy.size();
        return new int[]{
                (int) (sum / n),
                copy.get(n / 2),
                copy.get((int) (n * 0.95)),
                maxAbs,
                n
        };
    }

    public synchronized String snapshot() {
        int[] d = deltaStats();
        return "rate=" + sampleRate + " ch=" + channels
                + " pkts=" + packets + " queued=" + bytesQueued
                + " underruns=" + underruns
                + " head=" + playbackHead()
                + " clockUs=" + audioClockUs()
                + " avAvg=" + d[0] + " avP50=" + d[1]
                + " avP95=" + d[2] + " avMax=" + d[3]
                + " n=" + d[4]
                + " err=" + lastError;
    }

    private static byte[] defaultConfig() {
        ByteBuffer bb = ByteBuffer.allocate(12).order(ByteOrder.LITTLE_ENDIAN);
        bb.putInt(DEFAULT_RATE);
        bb.putShort((short) DEFAULT_CH);
        bb.putShort((short) 16);
        bb.putInt(DEFAULT_RATE * DEFAULT_CH * BYTES_PER_SAMPLE);
        return bb.array();
    }

    private void releaseQuiet() {
        if (track != null) {
            try { track.pause(); } catch (Exception ignored) {}
            try { track.flush(); } catch (Exception ignored) {}
            try { track.release(); } catch (Exception ignored) {}
            track = null;
        }
        configured = false;
    }
}
