package ai.toastovac.tv.live;

import android.util.Log;

import java.io.InputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicLong;

/** Persistent TCP client. Feeds the process-singleton decoder + audio sink. */
public final class LiveHdrReceiver {

    static final String TAG = "TOASTOVAC-LIVE";
    static final int DEFAULT_PORT = 8768;
    static final int STARTUP_AU_LOG = 16;

    private final String host;
    private final int port;
    private Thread thread;
    private final AtomicBoolean running = new AtomicBoolean();
    public volatile String status = "idle";
    public volatile long packets;
    public volatile long bytes;
    public volatile long audioPackets;
    public volatile long videoPackets;
    private final AtomicLong auIndex = new AtomicLong();

    public LiveHdrReceiver(String host, int port) {
        this.host = host;
        this.port = port;
    }

    public void start() {
        if (running.getAndSet(true)) return;
        thread = new Thread(this::loop, "live-hdr-rx");
        thread.start();
    }

    public void stop() {
        running.set(false);
        if (thread != null) thread.interrupt();
    }

    private void loop() {
        while (running.get()) {
            status = "connecting " + host + ":" + port;
            try (Socket s = new Socket()) {
                s.setReceiveBufferSize(1 << 20); // 1 MiB: absorb sender bursts
                s.connect(new InetSocketAddress(host, port), 4000);
                s.setTcpNoDelay(true);
                status = "connected";
                InputStream in = s.getInputStream();
                byte[] hdr = new byte[LiveProtocol.HEADER];
                while (running.get()) {
                    readFully(in, hdr, 0, hdr.length);
                    if (!LiveProtocol.looksLikeHeader(hdr, 0)) {
                        status = "bad magic";
                        break;
                    }
                    int len = le32(hdr, 16);
                    if (len < 0 || len > 4 * 1024 * 1024) {
                        status = "bad len " + len;
                        break;
                    }
                    byte[] payload = new byte[len];
                    readFully(in, payload, 0, len);
                    LiveProtocol pkt = LiveProtocol.parse(hdr, payload);
                    packets++;
                    bytes += len;
                    DecoderSession dec = DecoderSession.get();
                    AudioSession aud = AudioSession.get();
                    if (pkt.stream == LiveProtocol.STREAM_AUDIO) {
                        audioPackets++;
                        if ((pkt.flags & LiveProtocol.FLAG_CONFIG) != 0) {
                            aud.configure(pkt.payload);
                        } else if (pkt.payload.length > 0) {
                            aud.queue(pkt.payload, pkt.ptsUs);
                        }
                    } else if (pkt.payload.length > 0) {
                        videoPackets++;
                        long idx = auIndex.getAndIncrement();
                        if (idx < STARTUP_AU_LOG) {
                            Log.i(TAG, startupAu(idx, pkt));
                        }
                        dec.queue(pkt.payload, pkt.flags, pkt.ptsUs);
                        dec.drain();
                    }
                    status = "rx pkts=" + packets
                            + " v=" + videoPackets
                            + " a=" + audioPackets
                            + " " + dec.snapshot()
                            + " | " + aud.snapshot();
                    if (audioPackets > 0 && audioPackets % 250 == 0) {
                        Log.i(TAG, "AV " + aud.snapshot() + " " + dec.snapshot());
                    }
                }
            } catch (Exception e) {
                status = "err " + e.getMessage();
                Log.w(TAG, "rx", e);
            }
            if (!running.get()) break;
            try { Thread.sleep(800); } catch (InterruptedException ie) { break; }
        }
        status = "stopped";
    }

    private static String startupAu(long idx, LiveProtocol pkt) {
        StringBuilder sb = new StringBuilder();
        sb.append("AU ").append(idx)
          .append(" stream=").append(pkt.stream)
          .append(" flags=0x").append(String.format("%02x", pkt.flags))
          .append(" bytes=").append(pkt.payload.length)
          .append(" nals=[");
        int n = 0;
        int i = 0;
        while (i < pkt.payload.length - 1 && n < 10) {
            if (pkt.payload[i] != 0) { i++; continue; }
            int j = i;
            while (j < pkt.payload.length && pkt.payload[j] == 0) j++;
            if (j < pkt.payload.length && pkt.payload[j] == 1) {
                if (j - i >= 2 && j + 1 < pkt.payload.length) {
                    int t = (pkt.payload[j + 1] >> 1) & 0x3F;
                    if (n > 0) sb.append(',');
                    sb.append(name(t));
                    n++;
                }
                i = j + 1;
            } else {
                i = Math.max(i + 1, j);
            }
        }
        if (n == 0) sb.append("-");
        sb.append(']');
        return sb.toString();
    }

    private static String name(int t) {
        switch (t) {
            case 32: return "VPS";
            case 33: return "SPS";
            case 34: return "PPS";
            case 35: return "AUD";
            case 39: case 40: return "SEI";
            case 19: case 20: return "IDR";
            case 21: return "CRA";
            case 16: case 17: case 18: return "BLA";
            default: return t < 32 ? "V" + t : "N" + t;
        }
    }

    private static void readFully(InputStream in, byte[] b, int off, int n) throws Exception {
        int got = 0;
        while (got < n) {
            int r = in.read(b, off + got, n - got);
            if (r < 0) throw new java.io.EOFException();
            got += r;
        }
    }

    private static int le32(byte[] b, int off) {
        return (b[off] & 0xFF)
                | ((b[off + 1] & 0xFF) << 8)
                | ((b[off + 2] & 0xFF) << 16)
                | ((b[off + 3] & 0xFF) << 24);
    }
}
