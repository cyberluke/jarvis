package ai.toastovac.tv.vision;

import android.util.Log;

import java.io.File;
import java.io.FileWriter;
import java.io.PrintWriter;

/** Append-only pipeline log. Survives a process death; not a kernel panic. */
public final class PipelineLog {

    static final String TAG = "TOASTOVAC-4KUI";
    private static File file;

    public static synchronized void init(File dir) {
        if (dir == null) return;
        try {
            dir.mkdirs();
            file = new File(dir, "pipeline.log");
        } catch (Exception ignored) {
        }
    }

    public static void line(String msg) {
        Log.i(TAG, msg);
        File f = file;
        if (f == null) return;
        try (PrintWriter pw = new PrintWriter(new FileWriter(f, true))) {
            pw.println(System.currentTimeMillis() + " " + msg);
        } catch (Exception ignored) {
        }
    }
}
