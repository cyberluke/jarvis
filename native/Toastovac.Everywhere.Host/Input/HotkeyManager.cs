// Global hotkey interception. Uses a low-level keyboard hook so the chord is
// swallowed BEFORE the focused app sees it — otherwise Ctrl+Shift+Space's
// Space keypress reaches the editor and can clear the active selection. The
// table comes from the broker config (everywhere_hotkey_* keys). A chord we
// cannot own is reported, never silently remapped (§Toolbar behavior).
using System.Runtime.InteropServices;
using Microsoft.UI.Xaml;

namespace Toastovac.Everywhere.Host.Input;

public sealed class HotkeyManager : IDisposable
{
    // modifier bit flags matching the chord parser
    private const uint MOD_CTRL = 0x0002;
    private const uint MOD_SHIFT = 0x0004;
    private const uint MOD_ALT = 0x0001;

    private readonly Dictionary<string, (uint mods, uint vk)> _chords = new();
    private readonly Dictionary<string, ushort> _registered = new();
    private IntPtr _hook = IntPtr.Zero;
    private LowLevelKeyboardProc? _proc;
    private Action<string>? _onSlot;

    public IReadOnlyDictionary<string, ushort> Registered => _registered;
    public IReadOnlyList<string> Conflicts { get; private set; }
        = Array.Empty<string>();

    private delegate IntPtr LowLevelKeyboardProc(
        int nCode, IntPtr wParam, IntPtr lParam);

    /// <summary>Parse the table and install the low-level hook. ``onSlot``
    /// receives the slot name when its chord fires.</summary>
    public void Register(Window window,
        IReadOnlyDictionary<string, string> table, Action<string>? onSlot = null)
    {
        _onSlot = onSlot;
        foreach (var (slot, chord) in table)
        {
            if (TryParseChord(chord, out var mods, out var vk))
            {
                _chords[slot] = (mods, vk);
                _registered[slot] = (ushort)vk;
            }
        }
        InstallHook();
    }

    // Compatibility shim for the older SlotFor(id) lookup used by the WM_HOTKEY
    // path; with the low-level hook the slot is dispatched directly.
    public string? SlotFor(ushort id) => null;

    private void InstallHook()
    {
        _proc = HookCallback;
        _hook = SetWindowsHookEx(WH_KEYBOARD_LL, _proc,
            GetModuleHandle(null), 0);
    }

    /// <summary>While the toolbar is visible, single-letter action keys
    /// (R/P/A/E/T/U, Esc) are intercepted too — the toolbar is a
    /// non-activating overlay, so this is the only way the letters reach it.
    /// Scope is tight: only while ToolbarKeyCapture is true (§Toolbar
    /// behavior). When the user starts typing ordinary text the toolbar is
    /// dismissed and the key is NOT swallowed.</summary>
    public bool ToolbarKeyCapture { get; set; }

    /// <summary>Called for a toolbar-session letter key. Returns the action
    /// slot, or null when the key is not a toolbar action (so the key passes
    /// through and the toolbar should dismiss).</summary>
    public Func<string, string?>? ToolbarKeyResolver { get; set; }

    /// <summary>Forward a navigation key (arrows, Enter, Esc) to the toolbar
    /// while the toolbar session is active. Returns true when the key was
    /// consumed (so it is swallowed and never reaches the origin app).</summary>
    public Func<uint, bool>? ToolbarNavHandler { get; set; }

    /// <summary>While a modal overlay (OCR drag selector) is up, it cannot
    /// take keyboard focus (it is a non-activating window), so Esc is
    /// intercepted here. Set when the overlay opens, cleared when it closes.
    /// Returning true from the handler swallows the key.</summary>
    public Func<bool>? SessionEscapeHandler { get; set; }

    private IntPtr HookCallback(int nCode, IntPtr wParam, IntPtr lParam)
    {
        const int WM_KEYDOWN = 0x0100;
        if (nCode >= 0 && wParam == (IntPtr)WM_KEYDOWN)
        {
            var info = Marshal.PtrToStructure<KBDLLHOOKSTRUCT>(lParam);
            uint mods = CurrentModifiers();

            // Modal-overlay escape (OCR selector): Esc closes it.
            if (info.vkCode == 0x1B /* VK_ESCAPE */ && mods == 0
                && SessionEscapeHandler is not null)
            {
                bool handled = false;
                try { handled = SessionEscapeHandler(); }
                catch (Exception) { }
                if (handled)
                {
                    return (IntPtr)1;  // swallow Esc
                }
            }

            // Toolbar session: navigation keys (arrows, Enter, Space, Esc)
            // drive the toolbar; action letters fire the action. All are
            // swallowed so they never reach the origin app while the toolbar
            // is up (§Toolbar behavior: tightly scoped interception).
            if (ToolbarKeyCapture && mods == 0)
            {
                // Navigation keys -> the toolbar's TUI navigation.
                if (info.vkCode == 0x25 /* Left */ || info.vkCode == 0x27 /* Right */
                    || info.vkCode == 0x26 /* Up */ || info.vkCode == 0x28 /* Down */
                    || info.vkCode == 0x0D /* Enter */ || info.vkCode == 0x20 /* Space */
                    || info.vkCode == 0x1B /* Esc */)
                {
                    bool navHandled = false;
                    try { navHandled = ToolbarNavHandler?.Invoke(info.vkCode) ?? false; }
                    catch (Exception) { }
                    if (navHandled)
                    {
                        return (IntPtr)1;  // swallow the nav key
                    }
                }
                var ch = char.ToUpperInvariant((char)info.vkCode);
                var action = ToolbarKeyResolver?.Invoke(ch.ToString());
                if (action is not null)
                {
                    try { _onSlot?.Invoke(action); }
                    catch (Exception) { }
                    return (IntPtr)1;  // swallow the letter
                }
                // Not an action key: let it pass and let the app dismiss the
                // toolbar (the caller decides via the resolver returning null).
            }

            foreach (var (slot, chord) in _chords)
            {
                if (chord.vk == info.vkCode
                    && (mods & chord.mods) == chord.mods
                    // Require exactly the configured modifiers so Ctrl+Space
                    // does not fire a Ctrl+Shift+Space slot.
                    && (mods & (MOD_CTRL | MOD_SHIFT | MOD_ALT)) == chord.mods)
                {
                    try
                    {
                        _onSlot?.Invoke(slot);
                    }
                    catch (Exception)
                    {
                        // The handler must never take the hook down.
                    }
                    // Swallow the keypress: the origin app never sees the
                    // Space/letter, so it cannot clear the selection.
                    return (IntPtr)1;
                }
            }
        }
        return CallNextHookEx(_hook, nCode, wParam, lParam);
    }

    private static uint CurrentModifiers()
    {
        uint mods = 0;
        if ((GetAsyncKeyState(VK_CONTROL) & 0x8000) != 0) mods |= MOD_CTRL;
        if ((GetAsyncKeyState(VK_SHIFT) & 0x8000) != 0) mods |= MOD_SHIFT;
        if ((GetAsyncKeyState(VK_MENU) & 0x8000) != 0) mods |= MOD_ALT;
        return mods;
    }

    private static bool TryParseChord(string chord, out uint mods, out uint vk)
    {
        mods = 0;
        vk = 0;
        if (string.IsNullOrWhiteSpace(chord))
        {
            return false;
        }
        var parts = chord.Split('+', StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 0)
        {
            return false;
        }
        for (var i = 0; i < parts.Length - 1; i++)
        {
            switch (parts[i].Trim().ToLowerInvariant())
            {
                case "ctrl": mods |= MOD_CTRL; break;
                case "shift": mods |= MOD_SHIFT; break;
                case "alt": mods |= MOD_ALT; break;
            }
        }
        var key = parts[^1].Trim().ToLowerInvariant();
        vk = key switch
        {
            "space" => 0x20,
            "/" => 0xBF,
            "escape" or "esc" => 0x1B,
            _ => key.Length == 1 ? (uint)key.ToUpperInvariant()[0] : 0u,
        };
        return vk != 0;
    }

    public void UnregisterAll(Window window) => Dispose();

    public void Dispose()
    {
        if (_hook != IntPtr.Zero)
        {
            UnhookWindowsHookEx(_hook);
            _hook = IntPtr.Zero;
        }
    }

    private const int WH_KEYBOARD_LL = 13;
    private const int VK_CONTROL = 0x11;
    private const int VK_SHIFT = 0x10;
    private const int VK_MENU = 0x12;

    [StructLayout(LayoutKind.Sequential)]
    private struct KBDLLHOOKSTRUCT
    {
        public uint vkCode;
        public uint scanCode;
        public uint flags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook,
        LowLevelKeyboardProc lpfn, IntPtr hMod, uint dwThreadId);

    [DllImport("user32.dll")]
    private static extern bool UnhookWindowsHookEx(IntPtr hhk);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(
        IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern short GetAsyncKeyState(int vKey);

    [DllImport("kernel32.dll", CharSet = CharSet.Auto)]
    private static extern IntPtr GetModuleHandle(string? lpModuleName);
}
