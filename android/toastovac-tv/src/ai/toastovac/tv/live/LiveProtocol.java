package ai.toastovac.tv.live;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;

public final class LiveProtocol {
    public static final byte[] MAGIC = new byte[]{'T', 'Z', 'H', 'L'};
    public static final int VERSION = 2;
    public static final int HEADER = 20;

    public static final int STREAM_VIDEO = 0;
    public static final int STREAM_AUDIO = 1;
    public static final int STREAM_CONTROL = 2;

    public static final int FLAG_CONFIG = 0x01;
    public static final int FLAG_IDR = 0x02;
    public static final int FLAG_FRAME = 0x04;
    public static final int FLAG_EOS = 0x08;
    public static final int FLAG_DISCONTINUITY = 0x10;

    public int flags;
    public int stream;
    public long ptsUs;
    public byte[] payload;

    public static boolean looksLikeHeader(byte[] b, int off) {
        return b[off] == 'T' && b[off + 1] == 'Z' && b[off + 2] == 'H' && b[off + 3] == 'L';
    }

    public static LiveProtocol parse(byte[] header20, byte[] payload) {
        ByteBuffer bb = ByteBuffer.wrap(header20).order(ByteOrder.LITTLE_ENDIAN);
        byte[] mag = new byte[4];
        bb.get(mag);
        int ver = bb.get() & 0xFF;
        int flags = bb.get() & 0xFF;
        int stream = bb.getShort() & 0xFFFF;
        long pts = bb.getLong();
        int len = bb.getInt();
        if (ver != 1 && ver != VERSION) throw new IllegalArgumentException("ver " + ver);
        if (len != payload.length) throw new IllegalArgumentException("len");
        LiveProtocol p = new LiveProtocol();
        p.flags = flags;
        p.stream = stream;
        p.ptsUs = pts;
        p.payload = payload;
        return p;
    }
}
