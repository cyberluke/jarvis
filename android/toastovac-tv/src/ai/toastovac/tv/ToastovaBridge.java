package ai.toastovac.tv;

import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.widget.Toast;

import org.json.JSONObject;

import java.lang.ref.WeakReference;

import ai.toastovac.tv.vision.VisionRuntime;

/**
 * window.Toastovac — kept for offline.html + any leftover WebView pages.
 * Device info now includes hybrid render-target capabilities.
 */
public class ToastovaBridge {

    private final WeakReference<MainActivity> act;
    private final Handler handler = new Handler(Looper.getMainLooper());

    public ToastovaBridge(MainActivity activity) {
        this.act = new WeakReference<>(activity);
    }

    @JavascriptInterface
    public String getDeviceInfo() {
        try {
            MainActivity a = act.get();
            JSONObject o = new JSONObject();
            o.put("app", "toastovac-tv");
            o.put("version", "0.2.0");
            o.put("sdk", Build.VERSION.SDK_INT);
            o.put("model", Build.MODEL);
            if (a != null) {
                o.put("displayW", a.getResources().getDisplayMetrics().widthPixels);
                o.put("displayH", a.getResources().getDisplayMetrics().heightPixels);
                o.put("density", a.getResources().getDisplayMetrics().density);
                o.put("server", a.dashboardUrl());
            }
            o.put("render", new JSONObject(VisionRuntime.get().capabilitiesJson()));
            return o.toString();
        } catch (Exception e) {
            return "{}";
        }
    }

    @JavascriptInterface
    public String getRenderCapabilities() {
        return VisionRuntime.get().snapshot();
    }

    @JavascriptInterface
    public void toast(final String text) {
        handler.post(() -> {
            MainActivity a = act.get();
            if (a != null) Toast.makeText(a, text, Toast.LENGTH_SHORT).show();
            VisionRuntime.get().toast(text);
        });
    }

    @JavascriptInterface
    public void log(final String tag, final String msg) {
        android.util.Log.i("toastovac-" + tag, String.valueOf(msg));
    }

    @JavascriptInterface
    public void retry() {
        handler.post(() -> {
            MainActivity a = act.get();
            if (a != null && a.poller() != null) a.poller().start();
        });
    }

    @JavascriptInterface
    public void setServer(final String url) {
        handler.post(() -> {
            MainActivity a = act.get();
            if (a != null) a.setServerUrl(url);
        });
    }

    @JavascriptInterface
    public void pushCommand(final String json) {
        handler.post(() -> {
            try {
                MainActivity a = act.get();
                if (a != null && a.poller() != null) {
                    a.poller().handle(new JSONObject(json));
                }
            } catch (Exception ignored) {
            }
        });
    }
}
