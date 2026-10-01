// Entry point: starts the WinUI 3 message pump on EverywhereApp.
using System.Diagnostics;
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
        // Bind the host's lifetime to the process that spawned it
        // (Toastovac.exe / the Jarvis daemon): when the spawner exits — cleanly
        // or abruptly — the host exits with it, so the overlay can never
        // outlive the app (§Lifecycle). A host with no valid parent (manual
        // launch) keeps running.
        var watcher = new Thread(WatchParentProcess)
        {
            IsBackground = true,
            Name = "parent-lifecycle",
        };
        watcher.Start();

        // Never lose a crash: unhandled exceptions land in the host log
        // (everywhere_host.log) next to the structured lines, so a dead
        // overlay is diagnosable without a debugger.
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
        {
            try
            {
                EverywhereApp.Log($"FATAL {e.ExceptionObject?.GetType().Name}: "
                    + e.ExceptionObject?.ToString());
            }
            catch (Exception)
            {
            }
        };
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
        try
        {
            Microsoft.UI.Xaml.Application.Start(
                _ => new EverywhereApp());
        }
        catch (Exception ex)
        {
            try
            {
                EverywhereApp.Log($"APPLICATION START FAILED: {ex}");
            }
            catch (Exception)
            {
            }
            throw;
        }
    }

    private static void WatchParentProcess()
    {
        try
        {
            var parentPid = ParentProcessId();
            if (parentPid <= 0)
            {
                return;
            }
            var h = OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION,
                false, (uint)parentPid);
            if (h == IntPtr.Zero)
            {
                return;
            }
            try
            {
                // Signals when the parent process terminates.
                WaitForSingleObject(h, uint.MaxValue);
            }
            finally
            {
                CloseHandle(h);
            }
            try
            {
                EverywhereApp.Log("parent exited; host terminating");
            }
            catch (Exception)
            {
            }
            Environment.Exit(0);
        }
        catch (Exception)
        {
            // The watcher must never take the host down on its own.
        }
    }

    private static int ParentProcessId()
    {
        // NtQueryInformationProcess(ProcessBasicInformation) exposes the
        // parent PID without WMI; fields are IntPtr-sized on x64.
        var info = new PROCESS_BASIC_INFORMATION();
        var status = NtQueryInformationProcess(
            Process.GetCurrentProcess().Handle, 0 /* ProcessBasicInformation */,
            out info, Marshal.SizeOf<PROCESS_BASIC_INFORMATION>(), out _);
        return status == 0 ? info.InheritedFromUniqueProcessId.ToInt32() : 0;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_BASIC_INFORMATION
    {
        public IntPtr Reserved1;
        public IntPtr PebBaseAddress;
        public IntPtr Reserved2;
        public IntPtr Reserved3;
        public IntPtr UniqueProcessId;
        public IntPtr InheritedFromUniqueProcessId;
    }

    private const uint SYNCHRONIZE = 0x00100000;
    private const uint PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;

    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(IntPtr processHandle,
        int processInformationClass, out PROCESS_BASIC_INFORMATION info,
        int length, out int returnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(
        uint access, bool inheritHandle, uint processId);

    [DllImport("kernel32.dll")]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("user32.dll")]
    private static extern bool SetProcessDpiAwarenessContext(IntPtr value);
}
