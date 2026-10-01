package ai.toastovac.tv.vision;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaFormat;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.ByteBuffer;

/**
 * DashboardVideoTarget — TRUE 3840×2160 dashboard via the proven decoder path.
 *
 *   SceneGraph
 *     → Bitmap 3840×2160 (rasterized at native 4K, not 1080-upscaled)
 *     → ffmpeg HEVC Main Intra 3840×2160 (PC, one file, looped)
 *       OR on-device still (grey IDR) as bootstrap
 *     → c2.amlogic.hevc.decoder
 *     → decoder-owned UVM buffer → SurfaceView
 *     → HwcVideo DEVICE 3840×2160
 *
 * Hardware encoder is NOT used. Amlogic advertises
 * {@code c2.amlogic.hevc.encoder} max 1920×1088 — configuring 4K throws
 * IllegalArgumentException. The matching decoder already plays 2160p
 * source.mp4 as HwcVideo DEVICE 1:1.
 *
 * On-device we decode a pre-encoded 3840×2160 HEVC elementary stream
 * from the app files dir ({@code dash4k.h265}). The PC regenerates that
 * file from a scene snapshot whenever the dashboard is idle.
 */
public final class DashboardVideoTarget implements SurfaceHolder.Callback {

    static final String TAG = "TOASTOVAC-4KUI";

    public static final int W = 3840;
    public static final int H = 2160;
    public static final int FPS = 30;

    static final String MIME = "video/hevc";
    static final String DECODER = "c2.amlogic.hevc.decoder";

    private final SurfaceView output;
    private final RenderTarget target = RenderTarget.dashboard();
    private final ScenePainter painter = new ScenePainter();
    private final Handler handler = new Handler(Looper.getMainLooper());

    private static final Object PROCESS_GATE = new Object();
    private static volatile boolean PROCESS_STARTED;

    private MediaCodec decoder;
    private Thread codecThread;
    private volatile boolean running;
    private volatile boolean surfaceReady;
    private volatile boolean wantsPipeline;
    private volatile byte[] bitstream;
    private volatile File streamFile;

    public volatile long framesSubmitted;
    public volatile long framesDecodedWindow;
    public volatile long framesDropped;
    public volatile long renderNs;
    public volatile long encLatencyNs;
    public volatile long decLatencyNs;
    public volatile long e2eNs;
    public volatile long lastFpsWindowStart;
    public volatile int fps;
    public volatile String encoderName = "ffmpeg-hevc-intra";
    public volatile String decoderName = DECODER;
    public volatile String mime = MIME;
    public volatile int encodedW = W;
    public volatile int encodedH = H;
    public volatile int decodedW;
    public volatile int decodedH;
    public volatile int encodeW = W;
    public volatile int encodeH = H;
    public volatile boolean pipelineRunning;
    public volatile String pipelineReason = "init";

    public DashboardVideoTarget(SurfaceView output) {
        this.output = output;
        SurfaceHolder h = output.getHolder();
        h.setFixedSize(W, H);
        h.addCallback(this);
    }

    public void setStreamFile(File f) {
        this.streamFile = f;
    }

    public void start() {
        wantsPipeline = true;
        handler.post(this::updateMode);
    }

    public void stop() {
        wantsPipeline = false;
        handler.post(() -> stopPipeline("activity-stop"));
    }

    public void updateMode() {
        VisionRuntime rt = VisionRuntime.get();
        VisionRuntime.MediaHook m = rt.media();
        boolean playing = m != null && m.isPlaying() && m.videoWidth() > 0;
        if (playing) {
            handler.post(() -> {
                stopPipeline("media");
                output.setVisibility(android.view.View.GONE);
            });
        } else if (wantsPipeline && !pipelineRunning && surfaceReady) {
            if (output.getVisibility() != android.view.View.VISIBLE) {
                handler.post(() -> output.setVisibility(android.view.View.VISIBLE));
            }
            startPipeline();
        }
    }

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        surfaceReady = true;
        PipelineLog.line("output surfaceCreated");
        VisionRuntime.get().markAttached(RenderRole.DASHBOARD);
        if (wantsPipeline && !pipelineRunning) startPipeline();
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        surfaceReady = true;
        PipelineLog.line("output surfaceChanged " + width + "x" + height);
        // Do not restart the decoder on size/format callbacks — that
        // double-starts c2.amlogic.hevc.decoder and panics the kernel.
        if (wantsPipeline && !pipelineRunning) startPipeline();
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        surfaceReady = false;
        PipelineLog.line("output surfaceDestroyed (keep decoder)");
        // Intentionally do NOT stop the decoder here. BLAST/SurfaceView
        // recreates the surface during first present; tearing the decoder
        // down mid-stream kernel-panics this SoC.
    }

    private void startPipeline() {
        synchronized (PROCESS_GATE) {
            if (PROCESS_STARTED || pipelineRunning || !surfaceReady) {
                PipelineLog.line("startPipeline skipped processStarted=" + PROCESS_STARTED
                        + " running=" + pipelineRunning + " ready=" + surfaceReady);
                return;
            }
            PROCESS_STARTED = true;
            pipelineReason = "starting";
            running = true;
            lastFpsWindowStart = SystemClock.uptimeMillis();
            framesSubmitted = 0;
            framesDecodedWindow = 0;
            codecThread = new Thread(this::codecLoop, "4kui-dec");
            codecThread.start();
            pipelineRunning = true;
            PipelineLog.line("pipeline start (sw-hevc → amlogic decoder) PROCESS");
        }
    }

    private void stopPipeline(String reason) {
        // Never release the process-wide decoder. A second configure of
        // c2.amlogic.hevc.decoder kernel-panics this SoC.
        PipelineLog.line("stopPipeline ignored (" + reason + ") process decoder kept");
        pipelineReason = reason;
    }

    private void releaseDecoder() {
        if (decoder != null) {
            try { decoder.stop(); } catch (Exception ignored) {}
            try { decoder.release(); } catch (Exception ignored) {}
            decoder = null;
        }
    }

    private boolean configureDecoder() {
        try {
            Surface out = output.getHolder().getSurface();
            if (out == null || !out.isValid()) {
                pipelineReason = "no-output-surface";
                return false;
            }
            MediaFormat fmt = MediaFormat.createVideoFormat(MIME, W, H);
            fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, W * H);
            decoder = MediaCodec.createByCodecName(DECODER);
            decoder.configure(fmt, out, null, 0);
            decoder.start();
            decodedW = W;
            decodedH = H;
            pipelineReason = "running";
            PipelineLog.line("decoder started " + DECODER + " " + W + "x" + H);
            return true;
        } catch (Exception e) {
            pipelineReason = "decoder-error: " + e;
            PipelineLog.line("decoder FAILED " + e);
            releaseDecoder();
            return false;
        }
    }

    private byte[] loadOrBuildStream() {
        File f = streamFile;
        if (f != null && f.isFile() && f.length() > 32) {
            try {
                byte[] data = java.nio.file.Files.readAllBytes(f.toPath());
                PipelineLog.line("loaded " + f.getName() + " " + data.length + " bytes");
                return data;
            } catch (Exception e) {
                PipelineLog.line("load stream FAIL " + e);
            }
        }
        PipelineLog.line("no dash4k.h265 — encoding still via HevcStill");
        long t0 = System.nanoTime();
        Bitmap bmp = Bitmap.createBitmap(W, H, Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(bmp);
        VisionRuntime rt = VisionRuntime.get();
        rt.syncMedia();
        painter.paint(c, rt.graph, target);
        byte[] annexb;
        try {
            annexb = HevcStill.encode(bmp);
        } catch (Throwable e) {
            PipelineLog.line("HevcStill FAIL " + e);
            annexb = null;
        }
        bmp.recycle();
        renderNs = System.nanoTime() - t0;
        if (annexb != null) {
            PipelineLog.line("HevcStill " + annexb.length + " bytes in " + (renderNs / 1_000_000) + "ms");
            if (f != null) {
                try (FileOutputStream fos = new FileOutputStream(f)) {
                    fos.write(annexb);
                } catch (Exception ignored) {}
            }
        }
        return annexb;
    }

    private void codecLoop() {
        bitstream = loadOrBuildStream();
        if (bitstream == null || bitstream.length < 16) {
            pipelineReason = "no-bitstream";
            PipelineLog.line("codecLoop abort: no bitstream");
            running = false;
            pipelineRunning = false;
            return;
        }
        if (!configureDecoder()) {
            running = false;
            pipelineRunning = false;
            return;
        }
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        long ptsUs = 0;
        final long frameUs = 1_000_000L / FPS;
        while (running) {
            try {
                // Feed the still once, then keep draining. Re-queuing the
                // same IDR every frame makes the Amlogic decoder rebuild
                // its UVM chain and has panicked this box.
                if (framesSubmitted == 0) {
                    int inIdx = decoder.dequeueInputBuffer(50_000);
                    if (inIdx >= 0) {
                        ByteBuffer in = decoder.getInputBuffer(inIdx);
                        in.clear();
                        int n = Math.min(in.remaining(), bitstream.length);
                        in.put(bitstream, 0, n);
                        decoder.queueInputBuffer(inIdx, 0, n, ptsUs,
                                MediaCodec.BUFFER_FLAG_KEY_FRAME);
                        framesSubmitted++;
                        PipelineLog.line("queued IDR " + n + " bytes");
                    }
                }
                int outIdx;
                while ((outIdx = decoder.dequeueOutputBuffer(info, 0)) >= 0) {
                    decoder.releaseOutputBuffer(outIdx, true);
                    framesDecodedWindow++;
                    if (info.presentationTimeUs > 0) {
                        long nowUs = System.nanoTime() / 1000;
                        e2eNs = (nowUs - info.presentationTimeUs) * 1000;
                    }
                }
                if (outIdx == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                    MediaFormat f = decoder.getOutputFormat();
                    decodedW = f.getInteger(MediaFormat.KEY_WIDTH);
                    decodedH = f.getInteger(MediaFormat.KEY_HEIGHT);
                    PipelineLog.line("decoder output format " + decodedW + "x" + decodedH);
                }
                updateFps();
                try { Thread.sleep(1000 / FPS); } catch (InterruptedException e) { return; }
            } catch (Exception e) {
                PipelineLog.line("codecLoop " + e);
                framesDropped++;
                try { Thread.sleep(40); } catch (InterruptedException ie) { return; }
            }
        }
    }

    private void updateFps() {
        long now = SystemClock.uptimeMillis();
        long window = now - lastFpsWindowStart;
        if (window >= 1000) {
            fps = (int) (framesDecodedWindow * 1000L / window);
            lastFpsWindowStart = now;
            framesDecodedWindow = 0;
        }
    }
}
