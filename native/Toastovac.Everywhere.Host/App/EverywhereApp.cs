// The Everywhere application: a WinUI 3 Application subclass that owns the
// overlay window, the global hotkeys (WM_HOTKEY via a window subclass), the
// snapshot/transaction state, and the single pipe channel to the Python
// broker.
//
// Focus model (§Focus): the origin target identity is frozen into the
// snapshot at capture time and revalidated before any edit; the toolbar
// never takes ownership of the origin selection.
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.UI.Xaml;
using Toastovac.Everywhere.Host.Automation;
using Toastovac.Everywhere.Host.Input;
using Toastovac.Everywhere.Host.Pipe;
using Toastovac.Everywhere.Host.Protocol;
using Toastovac.Everywhere.Host.Replacement;
using Toastovac.Everywhere.Host.Windowing;

namespace Toastovac.Everywhere.Host.App;

public sealed class EverywhereApp : Application
{
    private Window? _window;
    private IntPtr _hwnd;
    private readonly HotkeyManager _hotkeys = new();

    /// <summary>The shared hotkey manager, exposed so overlay windows (the OCR
    /// selector) can register a session-Escape handler — those windows are
    /// non-activating and never receive keyboard focus.</summary>
    internal static HotkeyManager? Hotkeys { get; private set; }
    private readonly EverywherePipeClient _pipe = new();
    private Dictionary<string, object?> _snapshot = new();
    private System.Windows.Automation.AutomationElement? _originElement;
    private string _lastResult = "";
    private string _lastCapability = ReplaceCapability.CopyOnly;

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        _window = new Window { Title = "Toustovač Everywhere" };
        _window.Content = ToolbarWindow.Build(_window, DispatchSlot, Dismiss);
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(_window);
        Hotkeys = _hotkeys;
        // Borderless topmost tool window: no caption/min/max/close, no taskbar
        // entry, always above the origin app (§Toolbar behavior).
        Windowing.Chrome.MakeToolOverlay(_window);
        // The low-level hook swallows the chord before the origin app sees it,
        // so Ctrl+Shift+Space cannot clear the active selection.
        _hotkeys.Register(_window, Config.LoadHotkeyTable(),
            slot => _window.DispatcherQueue.TryEnqueue(() => DispatchSlot(slot)));
        foreach (var conflict in _hotkeys.Conflicts)
        {
            Log($"HOTKEY_CONFLICT {conflict}");
        }
        Log($"host started; hotkeys registered={_hotkeys.Registered.Count} " +
            $"conflicts={_hotkeys.Conflicts.Count}");
        // Prewarmed but hidden; a selection reveals it (§UX details).
    }

    private bool WmHotkey(IntPtr hwnd, uint msg, IntPtr wParam, IntPtr lParam)
    {
        if (msg == 0x0312 /* WM_HOTKEY */ &&
            _hotkeys.SlotFor((ushort)wParam.ToInt32()) is { } slot)
        {
            DispatchSlot(slot);
            return true;
        }
        return false;
    }

    private void DispatchSlot(string slot)
    {
        Log($"hotkey received: slot={slot}");
        switch (slot)
        {
            case "toolbar":
                var snap = SelectionObserver.CaptureFromFocus();
                var anchor = GeometryOf(snap ?? new Dictionary<string, object?>());
                Log($"toolbar slot: snapshot={((snap is null) ? "null" : "captured")} "
                    + $"anchor={anchor}");
                Freeze(snap);
                ToolbarWindow.ShowAt(_window, anchor);
                // Toolbar session: intercept action letters AND navigation
                // keys while the toolbar is up (it is a non-activating
                // overlay). Arrows/Enter/Esc drive the toolbar via the hook.
                _hotkeys.ToolbarKeyCapture = true;
                _hotkeys.ToolbarNavHandler = vk =>
                {
                    bool handled = false;
                    _window.DispatcherQueue.TryEnqueue(() =>
                    {
                        handled = ToolbarWindow.HandleNavKey(vk);
                    });
                    return true;  // always swallow nav keys while toolbar is up
                };
                _hotkeys.ToolbarKeyResolver = letter =>
                {
                    var map = new Dictionary<string, string>
                    {
                        ["R"] = "rewrite", ["P"] = "proofread",
                        ["A"] = "alternatives", ["E"] = "explain",
                        ["T"] = "translate", ["U"] = "prompt",
                        ["S"] = "subtitles", ["C"] = "coach",
                        ["O"] = "ocr",
                    };
                    if (map.TryGetValue(letter, out var action))
                    {
                        return action;
                    }
                    // Not an action key: dismiss and let the key through.
                    _window.DispatcherQueue.TryEnqueue(Dismiss);
                    return null;
                };
                break;
            case "rewrite": case "proofread": case "alternatives":
            case "explain": case "translate": case "prompt":
                RunAction(slot);
                break;
            case "subtitles":
                SubtitlesOverlay.Toggle(_window, _pipe);
                break;
            case "coach":
                CoachOverlay.Toggle(_window, _pipe);
                break;
            case "ocr":
                OcrOverlay.Toggle(_window, _pipe);
                break;
            case "help":
                ToolbarWindow.ShowHelp(_window);
                break;
            case "cancel":
                Dismiss();
                break;
        }
    }

    private void RunAction(string action)
    {
        if (_snapshot.Count == 0)
        {
            Freeze(SelectionObserver.CaptureFromFocus());
            if (_snapshot.Count == 0)
            {
                Log(FailureCodes.NoSelection);
                return;
            }
        }
        var request = new Dictionary<string, object?>
        {
            ["protocol"] = Framing.ProtocolId,
            ["kind"] = "action",
            ["request_id"] = NewRequestId(),
            ["action"] = action,
            ["snapshot_id"] = (string?)_snapshot["snapshot_id"] ?? "",
        };
        var reply = _pipe.RoundTrip(
            JsonSerializer.SerializeToElement(request));
        if (reply is null)
        {
            Log(FailureCodes.PipeDisconnected);
            return;
        }
        var root = reply.Value;
        if (root.TryGetProperty("failure", out var failure))
        {
            Log(failure.GetString() ?? FailureCodes.ProviderUnavailable);
            return;
        }
        _lastResult = root.TryGetProperty("result", out var r)
            ? r.GetString() ?? "" : "";
        _lastCapability = (string?)_snapshot["replace_capability"]
            ?? ReplaceCapability.CopyOnly;
        _lastAction = action;
        ToolbarWindow.ShowResult(_window, _lastResult, _lastCapability,
            Insert, CopyResult, action: action,
            onRetranslate: action == "translate" ? Retranslate : null);
    }

    private string _lastAction = "";

    /// <summary>Re-run the translate action with a new target language. The
    /// snapshot is unchanged; only the target language differs.</summary>
    private void Retranslate(string targetLanguage)
    {
        if (_snapshot.Count == 0) return;
        var request = new Dictionary<string, object?>
        {
            ["protocol"] = Framing.ProtocolId,
            ["kind"] = "action",
            ["request_id"] = NewRequestId(),
            ["action"] = "translate",
            ["snapshot_id"] = (string?)_snapshot["snapshot_id"] ?? "",
            ["target_language"] = targetLanguage,
        };
        var reply = _pipe.RoundTrip(
            JsonSerializer.SerializeToElement(request));
        if (reply is null) { Log(FailureCodes.PipeDisconnected); return; }
        var root = reply.Value;
        _lastResult = root.TryGetProperty("result", out var r)
            ? r.GetString() ?? "" : "";
        ToolbarWindow.ShowResult(_window, _lastResult, _lastCapability,
            Insert, CopyResult, action: "translate",
            onRetranslate: Retranslate);
    }

    private void Insert()
    {
        // Restore focus to the origin target before editing: the toolbar is a
        // non-activating overlay, so the click on Insert must not have moved
        // focus away from the app the user is editing (§Focus).
        try
        {
            _originElement?.SetFocus();
        }
        catch (Exception)
        {
            // Focus restore is best-effort.
        }
        // Revalidate the origin target (§Context transactions).
        var snapshotToken = TokenOf(_snapshot);
        var currentHash = TokenOf(SelectionObserver
            .Capture(_originElement ?? throw new InvalidOperationException()));
        var ok = !string.IsNullOrEmpty(snapshotToken)
            && snapshotToken == currentHash;
        if (!ok)
        {
            Log(FailureCodes.TargetChanged);
            return;
        }
        Replacer.Apply(_lastCapability, _lastResult, _originElement,
            restoreClipboard: true);
        ToolbarWindow.ResultPanelActive = false;
        Dismiss();
    }

    private void CopyResult() =>
        Replacer.Apply(ReplaceCapability.CopyOnly, _lastResult, null, false);

    private void Freeze(Dictionary<string, object?>? snapshot)
    {
        if (snapshot is null)
        {
            Log(FailureCodes.NoSelection);
            return;
        }
        _snapshot = snapshot;
        _originElement = System.Windows.Automation.AutomationElement
            .FocusedElement;
        var request = new Dictionary<string, object?>
        {
            ["protocol"] = Framing.ProtocolId,
            ["kind"] = "snapshot",
            ["request_id"] = NewRequestId(),
            ["snapshot"] = snapshot,
        };
        _pipe.RoundTrip(JsonSerializer.SerializeToElement(request));
    }

    private static string TokenOf(Dictionary<string, object?>? snapshot)
    {
        if (snapshot is not null
            && snapshot.TryGetValue("provider_token", out var tokObj)
            && tokObj is Dictionary<string, object?> tok
            && tok.TryGetValue("selected_text_hash", out var hash))
        {
            return (string?)hash ?? "";
        }
        return "";
    }

    private static (double, double)? GeometryOf(Dictionary<string, object?> s)
    {
        // Anchor under the selection: selection_bounds entries are
        // [x, y, right, bottom] in physical pixels. Show the toolbar just
        // below the selection's bottom edge (a small gap), left-aligned to
        // the selection start. Falls back to the mouse cursor when no
        // selection geometry is available (§Toolbar positioning).
        if (s.Count > 0 && s.TryGetValue("selection_bounds", out var b)
            && b is List<double[]> bounds && bounds.Count > 0
            && bounds[0].Length >= 4)
        {
            var x = bounds[0][0];
            var bottom = bounds[0][3];
            return (x, bottom + 8);
        }
        return MousePosition();
    }

    private static (double, double)? MousePosition()
    {
        if (GetCursorPos(out var pt))
        {
            return (pt.x, pt.y + 16);
        }
        return null;
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT pt);

    [System.Runtime.InteropServices.StructLayout(
        System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct POINT { public int x; public int y; }

    private void Dismiss()
    {
        // Leaving the toolbar session: stop intercepting letters.
        _hotkeys.ToolbarKeyCapture = false;
        _hotkeys.ToolbarKeyResolver = null;
        ToolbarWindow.Hide(_window);
    }

    private static string NewRequestId()
        => Guid.NewGuid().ToString("N")[..12];

    internal static void Log(string line)
    {
        // Structured log line: failure codes only, never selected text.
        System.Diagnostics.Debug.WriteLine($"[everywhere] {line}");
        // Persist next to the Python runtime log so hotkey/snapshot issues are
        // diagnosable without a debugger attached.
        try
        {
            var dir = System.IO.Path.Combine(
                System.Environment.GetFolderPath(
                    System.Environment.SpecialFolder.LocalApplicationData),
                "Jarvis");
            System.IO.Directory.CreateDirectory(dir);
            System.IO.File.AppendAllText(
                System.IO.Path.Combine(dir, "everywhere_host.log"),
                $"{System.DateTime.Now:HH:mm:ss} {line}\n");
        }
        catch (Exception)
        {
            // Logging must never throw.
        }
    }
}

// ── Window subclass for WM_HOTKEY ──────────────────────────────────────
internal static class Subclassing
{
    private delegate IntPtr SubclassProc(
        IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam,
        IntPtr uIdSubclass, IntPtr dwRefData);

    [DllImport("comctl32.dll", SetLastError = true)]
    private static extern bool SetWindowSubclass(
        IntPtr hWnd, SubclassProc pfnSubclass, IntPtr uIdSubclass,
        IntPtr dwRefData);

    [DllImport("comctl32.dll")]
    private static extern IntPtr DefSubclassProc(
        IntPtr hWnd, uint uMsg, IntPtr wParam, IntPtr lParam);

    private static SubclassProc? _proc;

    public static void Hook(IntPtr hwnd,
        Func<IntPtr, uint, IntPtr, IntPtr, bool> onMessage)
    {
        _proc = (hWnd, msg, wParam, lParam, id, data) =>
            onMessage(hWnd, msg, wParam, lParam)
                ? IntPtr.Zero
                : DefSubclassProc(hWnd, msg, wParam, lParam);
        SetWindowSubclass(hwnd, _proc, IntPtr.Zero, IntPtr.Zero);
    }
}
