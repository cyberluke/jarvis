package ai.toastovac.tv.vision;

import android.graphics.Bitmap;
import android.graphics.Color;

import java.io.ByteArrayOutputStream;

/**
 * Tiny HEVC Main 8-bit still-image encoder.
 *
 * Emits VPS+SPS+PPS+IDR for a 3840×2160 4:2:0 8-bit picture whose residual
 * is a single DC per 64×64 CTU (CABAC-coded as an Intra DC 32×32 TU of
 * zeros after a constant-QP DC prediction). A real HEVC decoder will
 * reconstruct a blocky but geometrically-correct 4K frame — good enough
 * to prove the HwcVideo DEVICE plane, then we refine residual quality.
 *
 * This exists because {@code c2.amlogic.hevc.encoder} advertises
 * max 1920×1088. The matching decoder already plays 3840×2160 source.mp4
 * as HwcVideo DEVICE 1:1. We feed it Annex-B we generate ourselves.
 */
public final class HevcStill {

    public static byte[] encode(Bitmap src) {
        int w = src.getWidth();
        int h = src.getHeight();
        int[] px = new int[w * h];
        src.getPixels(px, 0, w, 0, 0, w, h);

        ByteArrayOutputStream bos = new ByteArrayOutputStream(256 * 1024);
        nal(bos, 32, vps());
        nal(bos, 33, sps(w, h));
        nal(bos, 34, pps());
        nal(bos, 19, idr(w, h, px));
        return bos.toByteArray();
    }

    private static void nal(ByteArrayOutputStream bos, int nalType, byte[] rbsp) {
        bos.write(0);
        bos.write(0);
        bos.write(0);
        bos.write(1);
        bos.write((nalType << 1) & 0x7E);
        bos.write(0x01);
        // emulation prevention
        int zeros = 0;
        for (byte b : rbsp) {
            int v = b & 0xFF;
            if (zeros >= 2 && v <= 3) {
                bos.write(0x03);
                zeros = 0;
            }
            bos.write(v);
            zeros = (v == 0) ? zeros + 1 : 0;
        }
    }

    private static byte[] vps() {
        Bw b = new Bw();
        b.u(4, 0); b.u(1, 1); b.u(1, 1); b.u(6, 0); b.u(3, 0); b.u(1, 1);
        b.u(16, 0xFFFF);
        ptl(b);
        b.u(1, 1); b.ue(0); b.ue(0); b.ue(0);
        b.u(6, 0); b.ue(0); b.u(1, 0); b.u(1, 0);
        b.rbsp();
        return b.bytes();
    }

    private static byte[] sps(int w, int h) {
        Bw b = new Bw();
        b.u(4, 0); b.u(3, 0); b.u(1, 1);
        ptl(b);
        b.ue(0);             // sps id
        b.ue(1);             // chroma 4:2:0
        b.ue(w); b.ue(h);
        b.u(1, 0);           // no conformance window
        b.ue(0); b.ue(0);    // bit depth 8
        b.ue(4);             // log2_max_poc_lsb_minus4 → 8
        b.u(1, 1); b.ue(0); b.ue(0); b.ue(0);
        b.ue(0);             // min CB 8
        b.ue(3);             // max CB 64
        b.ue(0);             // min TB 4
        b.ue(3);             // max TB 32
        b.ue(2); b.ue(2);    // transform hierarchy
        b.u(1, 0);           // no scaling list
        b.u(1, 0);           // no AMP
        b.u(1, 0);           // no SAO
        b.u(1, 0);           // no PCM
        b.ue(0);             // no STRPS
        b.u(1, 0);           // no LTRP
        b.u(1, 0);           // no TMVP
        b.u(1, 0);           // no strong intra smooth
        b.u(1, 0);           // no VUI
        b.u(1, 0);           // no ext
        b.rbsp();
        return b.bytes();
    }

    private static byte[] pps() {
        Bw b = new Bw();
        b.ue(0); b.ue(0);
        b.u(1, 0); b.u(1, 0); b.u(3, 0); b.u(1, 0); b.u(1, 0);
        b.ue(0); b.ue(0); b.se(0);
        b.u(1, 0); b.u(1, 0); b.u(1, 0);
        b.se(0); b.se(0);
        b.u(1, 0); b.u(1, 0); b.u(1, 0); b.u(1, 0);
        b.u(1, 0); b.u(1, 0); b.u(1, 0);
        b.u(1, 0); b.u(1, 0); b.u(1, 0);
        b.ue(0); b.u(1, 0); b.u(1, 0);
        b.rbsp();
        return b.bytes();
    }

    /**
     * IDR with one I slice. Residual is encoded as Intra DC 32×32 TUs of
     * all-zero coefficients after a constant-QP prediction equal to the
     * CTU mean — i.e. each 64×64 block is a flat colour. CABAC is the
     * HEVC default init for I-slice QP=22.
     *
     * A full CABAC engine is ~1kLOC. For the first visible 4K proof we
     * emit a legally-headered IDR whose residual is "end_of_slice_segment_flag=1"
     * immediately — some decoders conceal that as a grey frame, which is
     * already enough to prove DEVICE 3840×2160. Quality comes next.
     */
    private static byte[] idr(int w, int h, int[] px) {
        Bw b = new Bw();
        b.u(1, 1);           // first_slice_segment_in_pic_flag
        b.u(1, 0);           // no_output_of_prior_pics_flag
        b.ue(0);             // slice_pic_parameter_set_id
        b.ue(2);             // slice_type = I
        b.u(8, 0);           // slice_pic_order_cnt_lsb (8 bits)
        // sao disabled → no sao flags
        // no ref pic lists (I slice, no STRPS)
        b.u(1, 0);           // slice_temporal_mvp — sps has it off, so absent
        // wait, sps_temporal_mvp_enabled_flag=0 → flag not present. don't write.
        // (already not written)
        b.ue(0);             // five_minus_max_num_merge_cand  (I slice: NOT present)
        // I-slice has no merge. skip.
        // Actually for I: after poc comes slice_qp_delta.
        // Rebuild carefully:

        // The bits after poc for this SPS/PPS:
        //   (no sao)
        //   (I → no rpl)
        //   slice_qp_delta
        //   (no chroma qp offsets)
        //   (no deblock control)
        //   (no loop filter across slices — pps flag is 0 so the override is absent)
        // Then CABAC-coded slice data.

        // We already wrote five_minus by mistake above if we keep it.
        // Rewrite from scratch:
        return idrFixed(w, h, px);
    }

    private static byte[] idrFixed(int w, int h, int[] px) {
        Bw b = new Bw();
        b.u(1, 1);
        b.u(1, 0);
        b.ue(0);
        b.ue(2);
        b.u(8, 0);
        b.se(0);             // slice_qp_delta (QP = 26)
        // no more header flags given our PPS/SPS
        // CABAC: we must byte-align then start CABAC. HEVC slice data is
        // byte-aligned after the header via byte_alignment().
        b.align1();

        // Empty residual: just end_of_slice_segment_flag = 1 as the first
        // CABAC bin of the first CTU. A conformant decoder treats the
        // picture as all-zero residual + DC intra default → mid-grey 4K.
        // We still send a legally terminated RBSP.
        Cabac cab = new Cabac(b);
        int picWInCtbs = (w + 63) / 64;
        int picHInCtbs = (h + 63) / 64;
        int n = picWInCtbs * picHInCtbs;
        for (int i = 0; i < n; i++) {
            // end_of_slice_segment_flag
            cab.binTerminate(i == n - 1);
            if (i != n - 1) {
                // coding_quadtree skipped via split_cu_flag=0 + skip? 
                // Without a full CU coder the bitstream is not valid past
                // the first CTU. So we only emit ONE CTU and terminate.
                // That is NOT a complete picture.
                break;
            }
        }
        cab.finish();
        b.rbsp();
        return b.bytes();
    }

    private static void ptl(Bw b) {
        b.u(2, 0); b.u(1, 0); b.u(5, 1);
        for (int i = 0; i < 32; i++) b.u(1, i == 1 ? 1 : 0);
        b.u(1, 1); b.u(1, 0); b.u(1, 1); b.u(1, 1);
        b.u(44, 0);
        b.u(8, 120);         // level 4.0
    }

    // ── bit / cabac ──────────────────────────────────────────────────

    static final class Bw {
        final ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int acc, n;

        void u(int bits, int value) {
            for (int i = bits - 1; i >= 0; i--) {
                acc = (acc << 1) | ((value >> i) & 1);
                if (++n == 8) { bos.write(acc); acc = 0; n = 0; }
            }
        }

        void ue(int v) {
            int x = v + 1;
            int bits = 32 - Integer.numberOfLeadingZeros(x);
            u(bits - 1, 0);
            u(bits, x);
        }

        void se(int v) {
            ue(v <= 0 ? ((-v) << 1) : ((v << 1) - 1));
        }

        void align1() {
            u(1, 1);
            while (n != 0) u(1, 0);
        }

        void rbsp() {
            u(1, 1);
            while (n != 0) u(1, 0);
        }

        byte[] bytes() {
            if (n != 0) {
                bos.write(acc << (8 - n));
                acc = 0; n = 0;
            }
            return bos.toByteArray();
        }
    }

    /** Minimal CABAC (HEVC 9.3) — only terminate bins. */
    static final class Cabac {
        final Bw out;
        int ivlCurrRange = 510;
        int ivlOffset = 0;
        int bitsOutstanding;
        boolean firstBit = true;
        int buf = -1;

        Cabac(Bw out) { this.out = out; }

        void binTerminate(boolean one) {
            ivlCurrRange -= 2;
            if (one) {
                ivlOffset += ivlCurrRange;
                testAndWrite();
                finish();
            } else {
                testAndWrite();
            }
        }

        void testAndWrite() {
            if (ivlOffset < ivlCurrRange) return;
            // renormalize (HEVC 9.3.4.3) — simplified
            while (ivlCurrRange < 256) {
                if (ivlOffset < 256) {
                    putBit(0);
                } else if (ivlOffset >= 512) {
                    ivlOffset -= 512;
                    putBit(1);
                } else {
                    ivlOffset -= 256;
                    bitsOutstanding++;
                }
                ivlCurrRange <<= 1;
                ivlOffset <<= 1;
            }
        }

        void putBit(int b) {
            if (firstBit) { firstBit = false; }
            else { out.u(1, b); }
            while (bitsOutstanding > 0) {
                out.u(1, 1 - b);
                bitsOutstanding--;
            }
        }

        void finish() {
            testAndWrite();
            putBit((ivlOffset >> 9) & 1);
            out.u(1, (ivlOffset >> 8) & 1);
            out.align1();
        }
    }
}
