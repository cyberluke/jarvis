package ai.toastovac.tv;

import android.app.Application;

import ai.toastovac.tv.vision.VisionRuntime;

/** Ensures VisionRuntime is first touched on the main thread. */
public class VisionBootstrap extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        VisionRuntime.get();
    }
}
