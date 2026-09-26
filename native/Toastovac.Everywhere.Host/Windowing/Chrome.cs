// Window chrome: turn the Everywhere window into a borderless, topmost,
// non-activating tool overlay (§Toolbar behavior, §Focus). No caption, no
// min/max/close, no taskbar entry, always above the origin app.
using System.Runtime.InteropServices;
using Microsoft.UI.Xaml;

namespace Toastovac.Everywhere.Host.Windowing;

public static class Chrome
{
    private const int GWL_STYLE = -16;
    private const int GWL_EXSTYLE = -20;

    private const long WS_CAPTION = 0x00C00000L;
    private const long WS_SYSMENU = 0x00080000L;
    private const long WS_MINIMIZEBOX = 0x00020000L;
    private const long WS_MAXIMIZEBOX = 0x00010000L;
    private const long WS_THICKFRAME = 0x00040000L;

    private const long WS_EX_TOOLWINDOW = 0x00000080L;
    private const long WS_EX_NOACTIVATE = 0x08000000L;
    private const long WS_EX_TOPMOST = 0x00000008L;
    private const long WS_EX_APPWINDOW = 0x00040000L;

    private static readonly IntPtr HWND_TOPMOST = new(-1);
    private const uint SWP_NOSIZE = 0x0001;
    private const uint SWP_NOMOVE = 0x0002;
    private const uint SWP_NOACTIVATE = 0x0010;
    private const uint SWP_FRAMECHANGED = 0x0020;
    private const uint SWP_SHOWWINDOW = 0x0040;

    /// <summary>Strip the caption/buttons and make the window a topmost,
    /// non-activating tool overlay. Idempotent.</summary>
    public static void MakeToolOverlay(Window window)
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        if (hwnd == IntPtr.Zero)
        {
            return;
        }

        long style = GetWindowLongPtr(hwnd, GWL_STYLE).ToInt64();
        style &= ~(WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
                   | WS_MAXIMIZEBOX | WS_THICKFRAME);
        SetWindowLongPtr(hwnd, GWL_STYLE, new IntPtr(style));

        long ex = GetWindowLongPtr(hwnd, GWL_EXSTYLE).ToInt64();
        ex |= WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST;
        ex &= ~WS_EX_APPWINDOW;  // keep it out of the taskbar / Alt+Tab
        SetWindowLongPtr(hwnd, GWL_EXSTYLE, new IntPtr(ex));

        SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED);
    }

    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtr(IntPtr hWnd, int nIndex);

    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")]
    private static extern IntPtr SetWindowLongPtr(
        IntPtr hWnd, int nIndex, IntPtr dwNewLong);

    /// <summary>Chroma-key transparency for the OCR drag selector: the given
    /// color becomes fully transparent so the desktop shows through, while the
    /// drawn selection rectangle / status pill / word boxes stay visible. The
    /// window stays hit-testable (NOT WS_EX_TRANSPARENT) so it receives the
    /// drag gesture.</summary>
    public static void MakeChromaKeyTransparent(Window window, byte r, byte g,
        byte b)
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        if (hwnd == IntPtr.Zero)
        {
            return;
        }
        long ex = GetWindowLongPtr(hwnd, GWL_EXSTYLE).ToInt64();
        ex |= WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_NOACTIVATE;
        SetWindowLongPtr(hwnd, GWL_EXSTYLE, new IntPtr(ex));
        // COLORREF is 0x00BBGGRR.
        uint colorKey = (uint)(r | (g << 8) | (b << 16));
        SetLayeredWindowAttributes(hwnd, colorKey, 0, LWA_COLORKEY);
    }

    /// <summary>Whole-window constant alpha (0-255). For a selection overlay
    /// on an HDR display where DirectComposition color keys don't apply, this
    /// makes the entire window (including its composited background) see-through
    /// while the drawn selection rectangle stays readable.</summary>
    public static void SetWholeWindowAlpha(Window window, byte alpha)
    {
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        if (hwnd == IntPtr.Zero)
        {
            return;
        }
        long ex = GetWindowLongPtr(hwnd, GWL_EXSTYLE).ToInt64();
        ex |= WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_NOACTIVATE;
        SetWindowLongPtr(hwnd, GWL_EXSTYLE, new IntPtr(ex));
        SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA);
    }

    private const long WS_EX_LAYERED = 0x00080000L;
    private const uint LWA_COLORKEY = 0x1;
    private const uint LWA_ALPHA = 0x2;

    [DllImport("user32.dll")]
    private static extern bool SetLayeredWindowAttributes(
        IntPtr hwnd, uint crKey, byte bAlpha, uint dwFlags);

    [DllImport("user32.dll")]
    private static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter,
        int X, int Y, int cx, int cy, uint uFlags);
}
