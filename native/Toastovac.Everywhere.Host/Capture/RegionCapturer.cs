// Physical-pixel region capture via GDI BitBlt (multi-monitor, mixed DPI).
// The bitmap buffer lives only for the recognition call and is closed by
// the OCR backend afterwards (§Screen Reading: image buffers are destroyed).
using System.Runtime.InteropServices;
using Toastovac.Everywhere.Host.Capture;
using Windows.Graphics.Imaging;

namespace Toastovac.Everywhere.Host.Capture;

public static class RegionCapturer
{
    public static SoftwareBitmap? CaptureRegion(PixelRect region)
    {
        var hdc = GetDC(nint.Zero);
        try
        {
            var width = region.Width;
            var height = region.Height;
            if (width <= 0 || height <= 0)
            {
                return null;
            }
            var memDc = CreateCompatibleDC(hdc);
            var hBitmap = CreateCompatibleBitmap(hdc, width, height);
            var old = SelectObject(memDc, hBitmap);
            BitBlt(memDc, 0, 0, width, height, hdc,
                region.X, region.Y, 0 /* SRCCOPY */);
            SelectObject(memDc, old);

            var header = new BITMAPINFOHEADER
            {
                biSize = (uint)Marshal.SizeOf<BITMAPINFOHEADER>(),
                biWidth = width,
                biHeight = height,
                biPlanes = 1,
                biBitCount = 32,
                biCompression = 0, // BI_RGB
            };
            var pixels = new byte[width * height * 4];
            var read = GetDIBits(memDc, hBitmap, 0, (uint)height, pixels,
                ref header, 0 /* DIB_RGB_COLORS */);
            DeleteObject(hBitmap);
            DeleteDC(memDc);
            if (read == 0)
            {
                return null;
            }
            // GDI rows are bottom-up; SoftwareBitmap expects top-down.
            var topDown = new byte[pixels.Length];
            var stride = width * 4;
            for (var y = 0; y < height; y++)
            {
                System.Array.Copy(pixels, (height - 1 - y) * stride,
                    topDown, y * stride, stride);
            }
            var bitmap = new SoftwareBitmap(
                BitmapPixelFormat.Bgra8, width, height,
                BitmapAlphaMode.Premultiplied);
            var buffer = System.Runtime.InteropServices.WindowsRuntime
                .WindowsRuntimeBufferExtensions.AsBuffer(
                    topDown, 0, topDown.Length);
            bitmap.CopyFromBuffer(buffer);
            return bitmap;
        }
        finally
        {
            ReleaseDC(nint.Zero, hdc);
        }
    }

    // ── Win32 GDI ─────────────────────────────────────────────────────
    [StructLayout(LayoutKind.Sequential)]
    private struct BITMAPINFOHEADER
    {
        public uint biSize;
        public int biWidth, biHeight;
        public ushort biPlanes, biBitCount;
        public uint biCompression, biSizeImage, biXPelsPerMeter,
            biYPelsPerMeter, biClrUsed, biClrImportant;
    }

    [DllImport("gdi32.dll")]
    private static extern nint CreateCompatibleDC(nint hdc);

    [DllImport("gdi32.dll")]
    private static extern nint CreateCompatibleBitmap(nint hdc, int w, int h);

    [DllImport("gdi32.dll")]
    private static extern nint SelectObject(nint hdc, nint obj);

    [DllImport("gdi32.dll")]
    private static extern bool BitBlt(nint dst, int x, int y, int w, int h,
        nint src, int sx, int sy, uint op);

    [DllImport("gdi32.dll")]
    private static extern int GetDIBits(nint hdc, nint hbm, uint start,
        uint lines, byte[] bits, ref BITMAPINFOHEADER info, uint usage);

    [DllImport("gdi32.dll")]
    private static extern bool DeleteObject(nint obj);

    [DllImport("gdi32.dll")]
    private static extern bool DeleteDC(nint hdc);

    [DllImport("user32.dll")]
    private static extern nint GetDC(nint hwnd);

    [DllImport("user32.dll")]
    private static extern int ReleaseDC(nint hwnd, nint hdc);
}
