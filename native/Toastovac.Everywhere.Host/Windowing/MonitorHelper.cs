// Per-monitor DPI + work-area helpers (§Toolbar positioning). Geometry is
// kept in physical pixels internally; conversion happens only at the WinUI
// presentation boundary. Overlays clamp against each monitor's work area,
// including negative coordinates and vertical taskbars.
using System.Runtime.InteropServices;

namespace Toastovac.Everywhere.Host.Windowing;

public static class MonitorHelper
{
    /// <summary>The primary screen's full physical-pixel size and the real
    /// work area, robust to DPI virtualization. ``GetSystemMetrics`` returns
    /// physical pixels even when the process is DPI-unaware, so the overlay
    /// spans the true screen width on a 4K/200% display.</summary>
    public static (double x, double y, double width, double height)
        PrimaryScreenPhysical()
    {
        // SM_CXSCREEN/SM_CYSCREEN: full physical screen; SPI_GETWORKAREA:
    // the work area (screen minus taskbar) in the process's coordinate space.
        double sw = GetSystemMetrics(0);   // SM_CXSCREEN
        double sh = GetSystemMetrics(1);   // SM_CYSCREEN
        var rc = new RECT();
        if (SystemParametersInfo(48 /*SPI_GETWORKAREA*/, 0, ref rc, 0))
        {
            // The work area is already in the process coordinate space; when
            // the process is DPI-virtualized it is in DIP, so scale it up to
            // physical using the screen metrics ratio.
            double workW = rc.right - rc.left;
            double workH = rc.bottom - rc.top;
            if (sw > 0 && workW > 0 && workW < sw)
            {
                double k = sw / workW;
                workW = sw;
                workH = workH * k;
            }
            return (0, 0, workW, workH);
        }
        return (0, 0, sw, sh);
    }

    public static (double x, double y, double width, double height)
        WorkAreaForPoint((double x, double y) point)
    {
        var index = IntPtr.Zero;
        try
        {
            index = GetMonitorFromPoint(new POINT
            { x = (int)point.x, y = (int)point.y }, 2 /* MONITOR_DEFAULTTONEAREST */);
        }
        catch (Exception)
        {
            return (0, 0, 1920, 1080);
        }
        var info = new MONITORINFOEXW();
        info.cbSize = (uint)Marshal.SizeOf<MONITORINFOEXW>();
        if (!GetMonitorInfoW(index, ref info))
        {
            return (0, 0, 1920, 1080);
        }
        var r = info.rcWork;
        return (r.left, r.top, r.right - r.left, r.bottom - r.top);
    }

    /// <summary>Clamp a physical-pixel rect inside the monitor work area.</summary>
    public static (double x, double y, double w, double h) ClampToWorkArea(
        (double x, double y, double w, double h) rect,
        (double x, double y, double width, double height) work)
    {
        var x = Math.Max(work.x, Math.Min(rect.x, work.x + work.width - rect.w));
        var y = Math.Max(work.y, Math.Min(rect.y, work.y + work.height - rect.h));
        return (x, y, rect.w, rect.h);
    }

    /// <summary>Physical -> DIP for the WinUI boundary (per-monitor aware).</summary>
    public static (double x, double y) PhysicalToDip(
        (int x, int y) physical, double dpiScale)
        => (physical.x / dpiScale, physical.y / dpiScale);

    /// <summary>The DPI scale factor (1.0 = 96 DPI) for a window, so overlay
    /// sizes follow the display's scaling (a fixed pixel size is tiny on a
    /// 4K/200% screen). Uses GetDpiForWindow (reliable for a topmost window),
    /// falling back to the monitor under a point, then 1.0.</summary>
    public static double DpiScaleForWindow(IntPtr hwnd,
        (double x, double y)? point = null, double fallback = 1.0)
    {
        try
        {
            if (hwnd != IntPtr.Zero)
            {
                uint dpi = GetDpiForWindow(hwnd);
                if (dpi > 0)
                {
                    return dpi / 96.0;
                }
            }
        }
        catch (Exception)
        {
        }
        if (point is { } pt)
        {
            try
            {
                var mon = GetMonitorFromPoint(new POINT
                { x = (int)pt.x, y = (int)pt.y }, 2 /* MONITOR_DEFAULTTONEAREST */);
                if (GetDpiForMonitor(mon, 0, out uint dx, out uint _) == 0
                    && dx > 0)
                {
                    return dx / 96.0;
                }
            }
            catch (Exception)
            {
            }
        }
        return fallback;
    }

    [DllImport("user32.dll")]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    [DllImport("shcore.dll")]
    private static extern int GetDpiForMonitor(
        IntPtr hmonitor, int dpiType, out uint dpiX, out uint dpiY);

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int x, y; }

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int left, top, right, bottom; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct MONITORINFOEXW
    {
        public uint cbSize;
        public RECT rcMonitor;
        public RECT rcWork;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
        public string szDevice;
    }

    [DllImport("user32.dll")]
    private static extern IntPtr GetMonitorFromPoint(POINT pt, uint flags);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int nIndex);

    [DllImport("user32.dll")]
    private static extern bool SystemParametersInfo(
        uint uiAction, uint uiParam, ref RECT pvParam, uint fWinIni);

    [DllImport("user32.dll")]
    private static extern bool GetMonitorInfoW(IntPtr hMonitor,
        ref MONITORINFOEXW lpiInfo);
}
