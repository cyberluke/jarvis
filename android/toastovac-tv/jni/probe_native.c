// DIAGNOSTIC ONLY (Phase 2 frozen). Do not launch NV12 / HW_VIDEO_DECODER
// variants on a living-room box — those kernel-panic Meson UVM.
// A2 probe: can an APP-created YUV surface (SurfaceView + ANativeWindow)
// become a DEVICE/HwcVideo layer on MesonHwc2 (Homatics Box R 4K Plus)?
//
// Public NDK API only. We set the window format + usage before lock, fill a
// pattern into the YUV planes with the CPU, and post. SurfaceFlinger then
// classifies the layer (CLIENT vs DEVICE) — we observe via dumpsys.
#include <jni.h>
#include <android/native_window.h>
#include <android/native_window_jni.h>
#include <string.h>
#include <stdio.h>

#define FMT_YV12 0x32315659u
#define FMT_NV12 0x3231564Eu
#define FMT_P010 0x30313050u

JNIEXPORT jstring JNICALL
Java_ai_toastovac_tv_YuvPlaneProbeActivity_nativeProbe(
    JNIEnv *env, jobject thiz,
    jobject surface, jint format, jlong usage,
    jint w, jint h, jstring name)
{
    const char *n = (*env)->GetStringUTFChars(env, name, NULL);
    char out[1024];
    out[0] = 0;
    int rc = 0;

    ANativeWindow *win = ANativeWindow_fromSurface(env, surface);
    if (!win) {
        snprintf(out, sizeof(out), "%s: NO_WINDOW", n);
        (*env)->ReleaseStringUTFChars(env, name, n);
        return (*env)->NewStringUTF(env, out);
    }

    // format+geometry via the one NDK call; usage is set from Java via
    // hidden Surface.setUsage (ANativeWindow_setUsage is not exported on A14).
    rc = ANativeWindow_setBuffersGeometry(win, w, h, format);
    if (rc != 0) {
        snprintf(out, sizeof(out), "%s: SETGEOM_FAIL rc=%d", n, rc);
        ANativeWindow_release(win);
        (*env)->ReleaseStringUTFChars(env, name, n);
        return (*env)->NewStringUTF(env, out);
    }

    ANativeWindow_Buffer buf;
    rc = ANativeWindow_lock(win, &buf, NULL);
    if (rc != 0) {
        snprintf(out, sizeof(out), "%s: LOCK_FAIL rc=%d", n, rc);
        ANativeWindow_release(win);
        (*env)->ReleaseStringUTFChars(env, name, n);
        return (*env)->NewStringUTF(env, out);
    }

    snprintf(out, sizeof(out), "%s: LOCKED fmt=0x%x w=%d h=%d stride=%d bits=%p",
             n, buf.format, buf.width, buf.height, buf.stride, buf.bits);

    if (buf.bits && buf.width > 0 && buf.height > 0 && buf.stride > 0) {
        switch (buf.format) {
        case FMT_YV12: {
            uint8_t *y = (uint8_t *)buf.bits;
            for (int r = 0; r < buf.height; r++)
                for (int c = 0; c < buf.stride; c++)
                    y[(size_t)r * buf.stride + c] = (uint8_t)((c / 16) % 2 ? 0x40 : 0xBF);
            uint8_t *v = y + (size_t)buf.stride * buf.height;
            uint8_t *u = v + ((size_t)buf.stride / 2) * ((size_t)buf.height / 2);
            memset(v, 0x80, ((size_t)buf.stride / 2) * ((size_t)buf.height / 2));
            memset(u, 0x80, ((size_t)buf.stride / 2) * ((size_t)buf.height / 2));
            strncat(out, " FILLED_YV12", sizeof(out) - strlen(out) - 1);
            break;
        }
        case FMT_NV12: {
            uint8_t *y = (uint8_t *)buf.bits;
            for (int r = 0; r < buf.height; r++)
                for (int c = 0; c < buf.stride; c++)
                    y[(size_t)r * buf.stride + c] = (uint8_t)((c / 16) % 2 ? 0x40 : 0xBF);
            uint8_t *uv = y + (size_t)buf.stride * buf.height;
            memset(uv, 0x80, (size_t)buf.stride * ((size_t)buf.height / 2));
            strncat(out, " FILLED_NV12", sizeof(out) - strlen(out) - 1);
            break;
        }
        case FMT_P010: {
            uint16_t *y = (uint16_t *)buf.bits;
            for (int r = 0; r < buf.height; r++)
                for (int c = 0; c < buf.stride; c++)
                    y[(size_t)r * buf.stride + c] = (uint16_t)((c / 16) % 2 ? 0x200 : 0x800);
            uint16_t *uv = y + (size_t)buf.stride * buf.height;
            for (size_t i = 0; i < (size_t)buf.stride * ((size_t)buf.height / 2); i++)
                uv[i] = 0x200;
            strncat(out, " FILLED_P010", sizeof(out) - strlen(out) - 1);
            break;
        }
        default: {
            uint8_t *p = (uint8_t *)buf.bits;
            size_t bytes = (size_t)buf.stride * (size_t)buf.height * 4;
            memset(p, 0x60, bytes);
            strncat(out, " FILLED_RGBA", sizeof(out) - strlen(out) - 1);
            break;
        }
        }
    }

    rc = ANativeWindow_unlockAndPost(win);
    if (rc != 0)
        snprintf(out + strlen(out), sizeof(out) - strlen(out), " POST_FAIL rc=%d", rc);
    else
        strncat(out, " POSTED", sizeof(out) - strlen(out) - 1);

    ANativeWindow_release(win);
    (*env)->ReleaseStringUTFChars(env, name, n);
    return (*env)->NewStringUTF(env, out);
}