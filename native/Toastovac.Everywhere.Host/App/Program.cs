// Entry point: starts the WinUI 3 message pump on EverywhereApp.
using System.Runtime.InteropServices;
using Microsoft.UI.Xaml;

namespace Toastovac.Everywhere.Host.App;

public static class Program
{
    // Per-Monitor V2: the process tracks the DPI of whatever monitor the
    // toolbar lands on, so the overlay scales on a 4K/200% display instead of
    // rendering at a quarter size. Set before any window is created.
    [STAThread]
    public static void Main()
    {
        try
        {
            // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (HANDLE)-4
            SetProcessDpiAwarenessContext(new IntPtr(-4));
        }
        catch (Exception)
        {
            // Older Windows: leave the default awareness; sizing falls back to
            // scale 1.0.
        }
        Microsoft.UI.Xaml.Application.Start(
            _ => new EverywhereApp());
    }

    [DllImport("user32.dll")]
    private static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
