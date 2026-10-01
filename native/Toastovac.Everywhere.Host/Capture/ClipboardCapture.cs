// Clipboard-based selection fallback for when UI Automation into the focused
// app hangs (a broken or hung UIA provider blocks every UIA client, so no
// amount of waiting on the UIA path helps). The fallback simulates Ctrl+C in
// the foreground window, reads the copied text, and restores the previous
// clipboard contents. Never used for console/terminal windows: Ctrl+C means
// SIGINT there, not copy.
using System.Runtime.InteropServices;
using System.Text;

namespace Toastovac.Everywhere.Host.Capture;

public static class ClipboardCapture
{
    private const uint CF_UNICODETEXT = 13;
    private const uint GMEM_MOVEABLE = 0x0002;

    private const int PollMs = 60;
    private const int MaxPolls = 30;      // ~1.8 s for the target to copy
    private const int ClipboardRetries = 8;

    /// <summary>Optional diagnostic hook (wired to the host log by
    /// EverywhereApp). Never receives clipboard TEXT — step outcomes only.</summary>
    public static Action<string>? Log { get; set; }

    /// <summary>Copy the focused window's selection via Ctrl+C and return the
    /// text (previous clipboard contents are restored before returning).
    /// Returns null when nothing was copied or the target cannot be used.</summary>
    public static string? TryCopySelection()
    {
        if (ForegroundIsConsoleOrSelf())
        {
            Log?.Invoke("clipboard skip=console-or-self");
            return null;
        }
        var previous = GetClipboardText();
        Log?.Invoke($"clipboard previous={(previous is null ? "none" : "text")}");
        if (!WithClipboard(() => EmptyClipboard()))
        {
            Log?.Invoke("clipboard fail=empty");
            return null;
        }
        Log?.Invoke("clipboard cleared");
        var sent = SendCtrlC();
        Log?.Invoke($"clipboard ctrl+c sent={sent}");
        // The clipboard was emptied above, so ANY text that appears next is
        // the copy result — there is no need (and no safe way) to compare
        // against the pre-copy contents: the selection may legitimately equal
        // the previous clipboard text.
        string? copied = null;
        for (var i = 0; i < MaxPolls; i++)
        {
            Thread.Sleep(PollMs);
            copied = GetClipboardText();
            if (!string.IsNullOrEmpty(copied))
            {
                Log?.Invoke($"clipboard poll#{i + 1} ok len={copied.Length}");
                break;
            }
            copied = null;
        }
        if (copied is null)
        {
            Log?.Invoke($"clipboard poll exhausted={MaxPolls}");
        }
        // Restore the pre-copy clipboard state so the fallback is invisible
        // to the user's own copy/paste.
        if (previous is not null)
        {
            Log?.Invoke(SetClipboardText(previous)
                ? "clipboard restored"
                : "clipboard restore=failed");
        }
        else
        {
            WithClipboard(() => EmptyClipboard());
        }
        return copied;
    }

    /// <summary>True when the foreground window is a console/terminal (Ctrl+C
    /// would interrupt the running command, not copy) or our own host window
    /// (there is nothing to copy there).</summary>
    private static bool ForegroundIsConsoleOrSelf()
    {
        var fg = GetForegroundWindow();
        if (fg == IntPtr.Zero)
        {
            return true;
        }
        GetWindowThreadProcessId(fg, out var pid);
        if (pid == Environment.ProcessId)
        {
            return true;
        }
        string name;
        try
        {
            name = System.Diagnostics.Process.GetProcessById((int)pid)
                .ProcessName;
        }
        catch (Exception)
        {
            return true;
        }
        return name.Equals("conhost", StringComparison.OrdinalIgnoreCase)
            || name.Equals("openconsole", StringComparison.OrdinalIgnoreCase)
            || name.Equals("windowsterminal", StringComparison.OrdinalIgnoreCase)
            || name.Equals("cmd", StringComparison.OrdinalIgnoreCase)
            || name.Equals("powershell", StringComparison.OrdinalIgnoreCase)
            || name.Equals("pwsh", StringComparison.OrdinalIgnoreCase);
    }

    private static string? GetClipboardText()
    {
        string? text = null;
        WithClipboard(() =>
        {
            var handle = GetClipboardData(CF_UNICODETEXT);
            if (handle == IntPtr.Zero)
            {
                return;
            }
            var ptr = GlobalLock(handle);
            if (ptr != IntPtr.Zero)
            {
                try
                {
                    text = Marshal.PtrToStringUni(ptr);
                }
                finally
                {
                    GlobalUnlock(handle);
                }
            }
        });
        return text;
    }

    private static bool SetClipboardText(string text)
    {
        var ok = false;
        WithClipboard(() =>
        {
            if (!EmptyClipboard())
            {
                return;
            }
            var bytes = Encoding.Unicode.GetBytes(text ?? "");
            var handle = GlobalAlloc(GMEM_MOVEABLE, (nuint)bytes.Length + 2);
            var ptr = GlobalLock(handle);
            if (ptr == IntPtr.Zero)
            {
                GlobalFree(handle);
                return;
            }
            Marshal.Copy(bytes, 0, ptr, bytes.Length);
            Marshal.WriteInt16(ptr, bytes.Length, 0);  // NUL terminator
            GlobalUnlock(handle);
            // SetClipboardData takes ownership of the handle on success.
            ok = SetClipboardData(CF_UNICODETEXT, handle) != IntPtr.Zero;
            if (!ok)
            {
                GlobalFree(handle);
            }
        });
        return ok;
    }

    private static uint SendCtrlC()
    {
        var inputs = new[]
        {
            Key(0x11, 0),                    // Ctrl down
            Key(0x43, 0),                    // C down
            Key(0x43, KEYEVENTF_KEYUP),      // C up
            Key(0x11, KEYEVENTF_KEYUP),      // Ctrl up
        };
        return SendInput((uint)inputs.Length, inputs,
            Marshal.SizeOf<INPUT>());
    }

    private static INPUT Key(ushort vk, uint flags)
    {
        var input = new INPUT { type = INPUT_KEYBOARD };
        input.U.ki.wVk = vk;
        input.U.ki.dwFlags = flags;
        return input;
    }

    /// <summary>Run a clipboard mutation with open/close around it, retrying
    /// when another process briefly holds the clipboard open.</summary>
    private static bool WithClipboard(Action body)
    {
        for (var i = 0; i < ClipboardRetries; i++)
        {
            if (OpenClipboard(IntPtr.Zero))
            {
                try
                {
                    body();
                    return true;
                }
                finally
                {
                    CloseClipboard();
                }
            }
            Thread.Sleep(30);
        }
        return false;
    }

    // ── Win32 ────────────────────────────────────────────────────────────
    private const uint INPUT_KEYBOARD = 1;
    private const uint KEYEVENTF_KEYUP = 0x0002;

    // The INPUT union MUST declare all three members: on x64 the union is
    // sized by MOUSEINPUT (32 bytes), so a one-member union produces the
    // wrong struct size and SendInput fails with ERROR_INVALID_PARAMETER.
    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT
    {
        public uint type;
        public InputUnion U;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public HARDWAREINPUT hi;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public nint dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MOUSEINPUT
    {
        public int dx;
        public int dy;
        public uint mouseData;
        public uint dwFlags;
        public uint time;
        public nint dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct HARDWAREINPUT
    {
        public uint uMsg;
        public ushort wParamL;
        public ushort wParamH;
    }

    [DllImport("user32.dll")]
    private static extern bool OpenClipboard(IntPtr hWndNewOwner);

    [DllImport("user32.dll")]
    private static extern bool CloseClipboard();

    [DllImport("user32.dll")]
    private static extern bool EmptyClipboard();

    [DllImport("user32.dll")]
    private static extern IntPtr GetClipboardData(uint uFormat);

    [DllImport("user32.dll")]
    private static extern IntPtr SetClipboardData(uint uFormat, IntPtr hMem);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GlobalAlloc(uint uFlags, nuint dwBytes);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GlobalLock(IntPtr hMem);

    [DllImport("kernel32.dll")]
    private static extern bool GlobalUnlock(IntPtr hMem);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GlobalFree(IntPtr hMem);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(
        IntPtr hWnd, out uint lpdwProcessId);

    [DllImport("user32.dll")]
    private static extern uint SendInput(uint nInputs, INPUT[] pInputs,
        int cbSize);
}