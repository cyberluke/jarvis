package ai.toastovac.tv;

import android.app.Activity;
import android.graphics.ImageFormat;
import android.hardware.HardwareBuffer;
import android.media.Image;
import android.media.ImageReader;
import android.os.Bundle;
import android.view.Surface;
import android.view.SurfaceControl;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.io.File;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.lang.reflect.Method;

/**
 * DIAGNOSTIC ONLY. Phase 2 firmware/UVM path is frozen.
 * Do not launch NV12 or HW_VIDEO_DECODER variants on a living-room box
 * (kernel panic). Safe default: YV12 app-owned buffers.
 *
 * A2 probe v4 — classifier discriminator hunt.
 * Question: which property of a user-created YUV buffer makes MesonHwc2 take
 * it as DEVICE (HwcVideo)? Candidates: format (NV12 vs YV12), dataspace
 * (BT709 vs SRGB), usage (decoder bits -> UVM, panics the kernel), origin
 * (decoder allocation vs app allocation).
 *
 * Routes:
 *  - gb: GraphicBuffer.create (raw usage) + Transaction.setBuffer(GraphicBuffer)
 *  - ir: ImageReader + JNI lock fill + acquire + setBuffer(HardwareBuffer)
 */
public class YuvPlaneProbeActivity extends Activity implements SurfaceHolder.Callback {

    static {
        System.loadLibrary("yuvplane");
    }

    native String nativeProbe(Surface surface, int format, long usage, int w, int h, String name);

    static final int FMT_YV12 = 0x32315659;
    static final int FMT_NV12 = 0x3231564E;

    static final long U_BASE = 0x00000824L;         // TEX|2D|COMP|OVERLAY-ish
    static final long U_VDEC = 0x00400000L;         // HW_VIDEO_DECODER (22)
    static final int DS_BT709 = 0x01 << 16 | 0x0;   // HAL_DATASPACE_V0_BT709

    static final class V {
        final String name;
        final int fmt;
        final long usage;
        final boolean gb;        // true = GraphicBuffer route, false = ImageReader route
        final int dataspace;    // -1 = none
        V(String n, int f, long u, boolean g, int ds) {
            name = n; fmt = f; usage = u; gb = g; dataspace = ds;
        }
    }

    // Default set is YV12-only. NV12 / bit-22 kernel-panic this SoC —
    // those stay in source as comments, launched only with --ei variant N
    // after an explicit operator decision (firmware path is frozen).
    final V[] VARIANTS = {
        new V("gb_yv12_base",   FMT_YV12, U_BASE, true, -1),
        new V("ir_yv12_ds709",  FMT_YV12, U_BASE, false, DS_BT709),
        // UNSAFE: new V("gb_nv12_base",   FMT_NV12, U_BASE, true, -1),
        // UNSAFE: new V("gb_nv12_dec22",  FMT_NV12, U_BASE | U_VDEC, true, -1),
    };

    private SurfaceView sv;
    private TextView status;
    private volatile boolean stop;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);

        sv = new SurfaceView(this);
        sv.getHolder().setFormat(FMT_YV12);
        sv.getHolder().addCallback(this);

        status = new TextView(this);
        status.setText("A2 v4 idle");
        status.setTextSize(18);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.addView(sv, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(status, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(root);
    }

    @Override
    public void surfaceCreated(SurfaceHolder h) {
        logLine("surfaceCreated " + h.getSurfaceFrame());
        int only = getIntent().getIntExtra("variant", -1);
        new Thread(() -> runProbe(only), "a2v4").start();
    }

    @Override
    public void surfaceChanged(SurfaceHolder h, int fmt, int w, int hh) {
        logLine("surfaceChanged " + w + "x" + hh);
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder h) {
        stop = true;
    }

    private void runProbe(int only) {
        try {
            Thread.sleep(500);
            for (int i = 0; i < VARIANTS.length; i++) {
                if (only >= 0 && only != i) continue;
                V v = VARIANTS[i];
                if (stop) break;
                runOnUiThread(() -> status.setText("probe: " + v.name));
                long t0 = System.currentTimeMillis();
                String r;
                try {
                    r = v.gb ? doGbvariant(v) : doIrVariant(v);
                } catch (Throwable e) {
                    r = "EX " + e;
                }
                logLine(v.name + ": " + r + " (" + (System.currentTimeMillis() - t0) + "ms)");
                Thread.sleep(1500);
            }
            logLine("DONE");
        } catch (Throwable e) {
            logLine("FATAL: " + e);
        }
    }

    private String doGbvariant(V v) throws Throwable {
        Class<?> gbCls = Class.forName("android.graphics.GraphicBuffer");
        Object gb = gbCls.getMethod("create", int.class, int.class, int.class, int.class)
                .invoke(null, 3840, 2160, v.fmt, (int) v.usage);
        if (gb == null) return "GB_NULL";
        int u = (int) gbCls.getMethod("getUsage").invoke(gb);
        SurfaceControl sc = new SurfaceControl.Builder()
                .setName(v.name)
                .setFormat(v.fmt)
                .setParent(sv.getSurfaceControl())
                .build();
        SurfaceControl.Transaction tx = new SurfaceControl.Transaction();
        tx.setLayer(sc, 1000);
        tx.setPosition(sc, 0, 0);
        // GraphicBuffer overload exists on device
        Method m = SurfaceControl.Transaction.class.getMethod("setBuffer", SurfaceControl.class, gbCls);
        m.invoke(tx, sc, gb);
        if (v.dataspace >= 0) {
            try {
                tx.getClass().getMethod("setDataspace", SurfaceControl.class, int.class)
                        .invoke(tx, sc, v.dataspace);
            } catch (Throwable t) {
                return "GB usage=0x" + Integer.toHexString(u) + " NODS " + t;
            }
        }
        tx.apply();
        return "GB usage=0x" + Integer.toHexString(u) + " SETBUFFER_OK";
    }

    private String doIrVariant(V v) throws Throwable {
        ImageReader reader = ImageReader.newInstance(3840, 2160, ImageFormat.YV12, 2, (int) v.usage);
        Surface prod = reader.getSurface();
        String fill = nativeProbe(prod, FMT_YV12, v.usage, 3840, 2160, v.name + "_fill");
        if (!fill.contains("POSTED")) {
            return "FILL_FAIL " + fill;
        }
        Image im = reader.acquireLatestImage();
        if (im == null) return "NO_IMAGE";
        HardwareBuffer hb = im.getHardwareBuffer();
        if (hb == null) {
            im.close();
            return "NO_HB";
        }
        String hbInfo = "hb fmt=" + hb.getFormat() + " w=" + hb.getWidth() + " h=" + hb.getHeight()
                + " usage=0x" + Long.toHexString(hb.getUsage());
        im.close();

        SurfaceControl sc = new SurfaceControl.Builder()
                .setName(v.name)
                .setFormat(FMT_YV12)
                .setParent(sv.getSurfaceControl())
                .build();
        SurfaceControl.Transaction tx = new SurfaceControl.Transaction();
        tx.setLayer(sc, 1000);
        tx.setPosition(sc, 0, 0);
        tx.setBuffer(sc, hb);
        if (v.dataspace >= 0) {
            try {
                tx.getClass().getMethod("setDataspace", SurfaceControl.class, int.class)
                        .invoke(tx, sc, v.dataspace);
            } catch (Throwable t) {
                return hbInfo + " NODS " + t;
            }
        }
        tx.apply();
        return hbInfo + " SETBUFFER_OK";
    }

    private void logLine(String line) {
        try {
            File f = new File(getExternalFilesDir(null), "yuvprobe.txt");
            try (PrintWriter pw = new PrintWriter(new FileWriter(f, true))) {
                pw.println(System.currentTimeMillis() + " " + line);
            }
        } catch (Throwable e) {
            android.util.Log.e("TOASTOVAC4K", "log fail", e);
        }
        android.util.Log.e("TOASTOVAC4K", line);
    }
}