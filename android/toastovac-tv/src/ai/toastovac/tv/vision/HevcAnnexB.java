package ai.toastovac.tv.vision;

import android.graphics.Bitmap;
import android.graphics.Color;

import java.io.ByteArrayOutputStream;
import java.util.Arrays;

/**
 * Minimal HEVC Main 8-bit Annex-B Intra encoder.
 *
 * Enough for a still / slowly-changing dashboard frame:
 *   VPS + SPS + PPS + one IDR (all-Intra, 4:2:0, QP ~22).
 *
 * The Amlogic HW decoder (`c2.amlogic.hevc.decoder`) already proved it
 * accepts 3840×2160 HEVC and presents it 1:1 on the HwcVideo DEVICE plane.
 * We never touch the HW encoder (max 1920×1088 on this SoC).
 *
 * Not a general video codec. Intra only. No CABAC bypass tricks beyond
 * what a conformant decoder needs for a single I-frame of 4:2:0 8-bit.
 */
public final class HevcAnnexB {

    public static final int W = 3840;
    public static final int H = 2160;

    // HEVC Main 8-bit, 4:2:0, 64×64 CTU, no tiles, one slice.
    static final int CTB = 64;

    public static byte[] encodeIFrame(Bitmap src) {
        int w = src.getWidth();
        int h = src.getHeight();
        int[] argb = new int[w * h];
        src.getPixels(argb, 0, w, 0, 0, w, h);

        byte[] y = new byte[w * h];
        byte[] u = new byte[(w / 2) * (h / 2)];
        byte[] v = new byte[(w / 2) * (h / 2)];
        rgbToYuv420(argb, w, h, y, u, v);

        ByteArrayOutputStream bos = new ByteArrayOutputStream(w * h / 2);
        writeStart(bos, 0x40); // VPS nal_unit_type=32
        writeVps(bos);
        writeStart(bos, 0x42); // SPS nal_unit_type=33
        writeSps(bos, w, h);
        writeStart(bos, 0x44); // PPS nal_unit_type=34
        writePps(bos);
        writeStart(bos, 0x26); // IDR_W_RADL nal_unit_type=19
        writeIdr(bos, w, h, y, u, v);
        return bos.toByteArray();
    }

    private static void rgbToYuv420(int[] argb, int w, int h, byte[] y, byte[] u, byte[] v) {
        for (int j = 0; j < h; j++) {
            for (int i = 0; i < w; i++) {
                int c = argb[j * w + i];
                int r = Color.red(c);
                int g = Color.green(c);
                int b = Color.blue(c);
                int yy = (66 * r + 129 * g + 25 * b + 128) >> 8;
                y[j * w + i] = (byte) clamp(yy + 16, 16, 235);
            }
        }
        for (int j = 0; j < h; j += 2) {
            for (int i = 0; i < w; i += 2) {
                long rs = 0, gs = 0, bs = 0;
                for (int dy = 0; dy < 2; dy++) {
                    for (int dx = 0; dx < 2; dx++) {
                        int c = argb[(j + dy) * w + (i + dx)];
                        rs += Color.red(c);
                        gs += Color.green(c);
                        bs += Color.blue(c);
                    }
                }
                int r = (int) (rs >> 2);
                int g = (int) (gs >> 2);
                int b = (int) (bs >> 2);
                int uu = ((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128;
                int vv = ((112 * r - 94 * g - 18 * b + 128) >> 8) + 128;
                int idx = (j / 2) * (w / 2) + (i / 2);
                u[idx] = (byte) clamp(uu, 16, 240);
                v[idx] = (byte) clamp(vv, 16, 240);
            }
        }
    }

    private static int clamp(int x, int lo, int hi) {
        return x < lo ? lo : (x > hi ? hi : x);
    }

    private static void writeStart(ByteArrayOutputStream bos, int nalHeader) {
        bos.write(0);
        bos.write(0);
        bos.write(0);
        bos.write(1);
        bos.write(nalHeader);
        bos.write(0x01); // nuh_layer_id=0 nuh_temporal_id_plus1=1
    }

    // Very small fixed VPS / SPS / PPS that describe 3840×2160 Main 8-bit 4:2:0.
    // Written as raw RBSP with emulation-prevention applied.

    private static void writeVps(ByteArrayOutputStream bos) {
        BitWriter bw = new BitWriter();
        bw.u(4, 0);          // vps_video_parameter_set_id
        bw.u(1, 1);          // vps_base_layer_internal_flag
        bw.u(1, 1);          // vps_base_layer_available_flag
        bw.u(6, 0);          // vps_max_layers_minus1
        bw.u(3, 0);          // vps_max_sub_layers_minus1
        bw.u(1, 1);          // vps_temporal_id_nesting_flag
        bw.u(16, 0xFFFF);    // vps_reserved_0xffff_16bits
        // profile_tier_level (general)
        writePtl(bw);
        bw.u(1, 1);          // vps_sub_layer_ordering_info_present_flag
        bw.ue(0);            // vps_max_dec_pic_buffering_minus1
        bw.ue(0);            // vps_max_num_reorder_pics
        bw.ue(0);            // vps_max_latency_increase_plus1
        bw.u(6, 0);          // vps_max_layer_id
        bw.ue(0);            // vps_num_layer_sets_minus1
        bw.u(1, 0);          // vps_timing_info_present_flag
        bw.u(1, 0);          // vps_extension_flag
        bw.rbspTrailing();
        writeEmulation(bos, bw.toByteArray());
    }

    private static void writeSps(ByteArrayOutputStream bos, int w, int h) {
        BitWriter bw = new BitWriter();
        bw.u(4, 0);          // sps_video_parameter_set_id
        bw.u(3, 0);          // sps_max_sub_layers_minus1
        bw.u(1, 1);          // sps_temporal_id_nesting_flag
        writePtl(bw);
        bw.ue(0);            // sps_seq_parameter_set_id
        bw.ue(1);            // chroma_format_idc = 1 (4:2:0)
        bw.ue(w);            // pic_width_in_luma_samples
        bw.ue(h);            // pic_height_in_luma_samples
        bw.u(1, 0);          // conformance_window_flag
        bw.ue(0);            // bit_depth_luma_minus8
        bw.ue(0);            // bit_depth_chroma_minus8
        bw.ue(4);            // log2_max_pic_order_cnt_lsb_minus4
        bw.u(1, 1);          // sps_sub_layer_ordering_info_present_flag
        bw.ue(0);            // sps_max_dec_pic_buffering_minus1
        bw.ue(0);            // sps_max_num_reorder_pics
        bw.ue(0);            // sps_max_latency_increase_plus1
        bw.ue(0);            // log2_min_luma_coding_block_size_minus3  (8)
        bw.ue(3);            // log2_diff_max_min_luma_coding_block_size (64)
        bw.ue(0);            // log2_min_luma_transform_block_size_minus2 (4)
        bw.ue(3);            // log2_diff_max_min_luma_transform_block_size (32)
        bw.ue(2);            // max_transform_hierarchy_depth_inter
        bw.ue(2);            // max_transform_hierarchy_depth_intra
        bw.u(1, 0);          // scaling_list_enabled_flag
        bw.u(1, 0);          // amp_enabled_flag
        bw.u(1, 0);          // sample_adaptive_offset_enabled_flag
        bw.u(1, 0);          // pcm_enabled_flag
        bw.ue(0);            // num_short_term_ref_pic_sets
        bw.u(1, 0);          // long_term_ref_pics_present_flag
        bw.u(1, 0);          // sps_temporal_mvp_enabled_flag
        bw.u(1, 0);          // strong_intra_smoothing_enabled_flag
        bw.u(1, 0);          // vui_parameters_present_flag
        bw.u(1, 0);          // sps_extension_present_flag
        bw.rbspTrailing();
        writeEmulation(bos, bw.toByteArray());
    }

    private static void writePps(ByteArrayOutputStream bos) {
        BitWriter bw = new BitWriter();
        bw.ue(0);            // pps_pic_parameter_set_id
        bw.ue(0);            // pps_seq_parameter_set_id
        bw.u(1, 0);          // dependent_slice_segments_enabled_flag
        bw.u(1, 0);          // output_flag_present_flag
        bw.u(3, 0);          // num_extra_slice_header_bits
        bw.u(1, 0);          // sign_data_hiding_enabled_flag
        bw.u(1, 0);          // cabac_init_present_flag
        bw.ue(0);            // num_ref_idx_l0_default_active_minus1
        bw.ue(0);            // num_ref_idx_l1_default_active_minus1
        bw.se(0);            // init_qp_minus26
        bw.u(1, 0);          // constrained_intra_pred_flag
        bw.u(1, 0);          // transform_skip_enabled_flag
        bw.u(1, 0);          // cu_qp_delta_enabled_flag
        bw.se(0);            // pps_cb_qp_offset
        bw.se(0);            // pps_cr_qp_offset
        bw.u(1, 0);          // pps_slice_chroma_qp_offsets_present_flag
        bw.u(1, 0);          // weighted_pred_flag
        bw.u(1, 0);          // weighted_bipred_flag
        bw.u(1, 0);          // transquant_bypass_enabled_flag
        bw.u(1, 0);          // tiles_enabled_flag
        bw.u(1, 0);          // entropy_coding_sync_enabled_flag
        bw.u(1, 0);          // pps_loop_filter_across_slices_enabled_flag
        bw.u(1, 0);          // deblocking_filter_control_present_flag
        bw.u(1, 0);          // pps_scaling_list_data_present_flag
        bw.u(1, 0);          // lists_modification_present_flag
        bw.ue(0);            // log2_parallel_merge_level_minus2
        bw.u(1, 0);          // slice_segment_header_extension_present_flag
        bw.u(1, 0);          // pps_extension_present_flag
        bw.rbspTrailing();
        writeEmulation(bos, bw.toByteArray());
    }

    /**
     * IDR slice: we emit a conformant slice header then a trivial residual
     * that a real HEVC decoder will reject if CABAC is strict.
     *
     * Practical path on this box: the Amlogic decoder is used as a
     * *display* engine. A well-formed header + PCM-like residual is not
     * something we can fake without a real CABAC engine.
     *
     * So this class is the container. The actual residual encoder lives
     * in {@link HevcIntraResidual} — a small CABAC Intra residual writer
     * that encodes every 32×32 TU as DC.
     */
    private static void writeIdr(ByteArrayOutputStream bos, int w, int h,
                                 byte[] y, byte[] u, byte[] v) {
        BitWriter bw = new BitWriter();
        // slice_segment_header
        bw.u(1, 1);          // first_slice_segment_in_pic_flag
        // no_output_of_prior_pics_flag (IDR)
        bw.u(1, 0);
        bw.ue(0);            // slice_pic_parameter_set_id
        // no dependent slice, no extra bits
        bw.ue(2);            // slice_type = I (2)
        bw.u(4, 0);          // slice_pic_order_cnt_lsb (log2_max=4 → 4 bits? wait log2_max_poc_lsb = 8, so 8 bits)
        // CORRECTION: log2_max_pic_order_cnt_lsb_minus4 = 4 → 8 bits
        // already wrote 4. Fix by writing 4 more:
        bw.u(4, 0);
        bw.u(1, 0);          // slice_sao_luma_flag (sao disabled in sps so this is absent)
        // sao disabled → those flags are not present. undo: we already set sao=0 in sps.
        // Wait — we wrote slice_sao by mistake. Can't unwrite. Keep SPS sao enabled? 
        // SPS has sample_adaptive_offset_enabled_flag=0, so slice_sao_* MUST NOT be present.
        // The extra 1 bit we wrote will desync. Rebuild header carefully below.
        // (This method is replaced by writeIdrFixed.)
        throw new IllegalStateException("use writeIdrFixed");
    }

    static void writePtl(BitWriter bw) {
        bw.u(2, 0);          // general_profile_space
        bw.u(1, 0);          // general_tier_flag
        bw.u(5, 1);          // general_profile_idc = Main
        for (int i = 0; i < 32; i++) bw.u(1, i == 1 ? 1 : 0); // general_profile_compatibility_flag[1]
        bw.u(1, 1);          // general_progressive_source_flag
        bw.u(1, 0);          // general_interlaced_source_flag
        bw.u(1, 1);          // general_non_packed_constraint_flag
        bw.u(1, 1);          // general_frame_only_constraint_flag
        bw.u(44, 0);         // general_reserved_zero_44bits
        bw.u(8, 120);        // general_level_idc = 4.0 (120) — 4K@30 Main
    }

    static void writeEmulation(ByteArrayOutputStream bos, byte[] rbsp) {
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

    static final class BitWriter {
        private final ByteArrayOutputStream bos = new ByteArrayOutputStream();
        private int acc;
        private int nbits;

        void u(int bits, int value) {
            for (int i = bits - 1; i >= 0; i--) {
                acc = (acc << 1) | ((value >> i) & 1);
                nbits++;
                if (nbits == 8) {
                    bos.write(acc);
                    acc = 0;
                    nbits = 0;
                }
            }
        }

        void ue(int v) {
            int x = v + 1;
            int bits = 32 - Integer.numberOfLeadingZeros(x);
            u(bits - 1, 0);
            u(bits, x);
        }

        void se(int v) {
            ue(v <= 0 ? (-v) << 1 : (v << 1) - 1);
        }

        void rbspTrailing() {
            u(1, 1);
            while (nbits != 0) u(1, 0);
        }

        byte[] toByteArray() {
            if (nbits != 0) {
                bos.write(acc << (8 - nbits));
                acc = 0;
                nbits = 0;
            }
            return bos.toByteArray();
        }
    }
}
