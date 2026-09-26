// Replacement providers (§Replace capabilities). The host applies the one
// mechanism the target actually supports; no silent fallback between them.
// The clipboard transaction restores the previous clipboard only when no
// other writer touched it meanwhile.
using System.Runtime.InteropServices;
using Microsoft.UI.Xaml;
using Windows.ApplicationModel.DataTransfer;

namespace Toastovac.Everywhere.Host.Replacement;

public static class Replacer
{
    public static bool Apply(string capability, string result,
        object? originElement, bool restoreClipboard)
    {
        switch (capability)
        {
            case Protocol.ReplaceCapability.SemanticRangeEdit:
                // The VSIX / terminal bridge owns these edits; the host
                // forwards the already-revalidated result unchanged.
                return SemanticRangeEdit(result);
            case Protocol.ReplaceCapability.ValuePatternEdit:
                return ValuePatternEdit(result, originElement);
            case Protocol.ReplaceCapability.UnicodeInputReplace:
                return UnicodeInputReplace(result);
            case Protocol.ReplaceCapability.ClipboardTransactionReplace:
                return ClipboardTransactionReplace(result, restoreClipboard);
            case Protocol.ReplaceCapability.CopyOnly:
            default:
                CopyOnly(result);
                return true;
        }
    }

    private static bool ValuePatternEdit(string result, object? element)
    {
        if (element is not System.Windows.Automation.AutomationElement ae)
        {
            return false;
        }
        try
        {
            if (ae.GetCurrentPattern(
                    System.Windows.Automation.ValuePatternIdentifiers.Pattern)
                is System.Windows.Automation.ValuePattern value)
            {
                value.SetValue(result);
                return true;
            }
        }
        catch (Exception)
        {
            return false;
        }
        return false;
    }

    private static bool UnicodeInputReplace(string result)
    {
        // Selected text is already the active selection in the origin
        // control; SendInput in Unicode mode replaces it atomically.
        // One Unicode char per event keeps the scan-code path reliable.
        var list = new List<INPUT>();
        foreach (var ch in result)
        {
            var input = new INPUT { type = INPUT_KEYBOARD };
            input.U.ki.wVk = 0;
            input.U.ki.wScan = ch;
            input.U.ki.dwFlags = KEYEVENTF_UNICODE;
            list.Add(input);
        }
        return SendInput((uint)list.Count, list.ToArray(),
            Marshal.SizeOf<INPUT>()) > 0;
    }

    private static bool ClipboardTransactionReplace(string result,
        bool restore)
    {
        var previous = ClipboardSnapshot();
        CopyOnly(result);
        PasteFromClipboard();
        if (restore)
        {
            var now = ClipboardSnapshot();
            if (now == result && previous != result)
            {
                CopyOnly(previous ?? "");
            }
        }
        return true;
    }

    private static void CopyOnly(string result)
    {
        var package = new DataPackage();
        package.SetText(result ?? "");
        Clipboard.SetContent(package);
    }

    private static string? ClipboardSnapshot()
    {
        try
        {
            var view = Clipboard.GetContent();
            return view.Contains(StandardDataFormats.Text)
                ? view.GetTextAsync().GetAwaiter().GetResult()
                : null;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static void PasteFromClipboard()
    {
        // Ctrl+V via the Unicode path is unreliable on some controls; the
        // standard virtual-key pair is the documented paste gesture.
        SendVirtualKeyPair(0x56); // VK 'V' with Ctrl held by the app focus
    }

    private static bool SemanticRangeEdit(string result)
    {
        // VSIX/bridge edits travel on the existing terminal bridge pipe as
        // insert_request frames; the host writes them through unchanged.
        return TerminalBridgeInsert.Write(result);
    }

    private static void SendVirtualKeyPair(uint vk)
    {
        var inputs = new INPUT[4];
        inputs[0] = KeyInput(vk, 0);
        inputs[1] = KeyInput(vk, KEYEVENTF_KEYUP);
        inputs[2] = KeyInput(0x11, 0); // Ctrl
        inputs[3] = KeyInput(0x11, KEYEVENTF_KEYUP);
        _ = inputs;
        // Minimal, ordered: Ctrl down, V down, V up, Ctrl up.
        var ordered = new[]
        {
            KeyInput(0x11, 0),
            KeyInput(vk, 0),
            KeyInput(vk, KEYEVENTF_KEYUP),
            KeyInput(0x11, KEYEVENTF_KEYUP),
        };
        SendInput((uint)ordered.Length, ordered, Marshal.SizeOf<INPUT>());
    }

    private static INPUT KeyInput(uint vk, uint flags)
    {
        var input = new INPUT { type = INPUT_KEYBOARD };
        input.U.ki.wVk = (ushort)vk;
        input.U.ki.dwFlags = flags;
        return input;
    }

    // ── Win32 (real KEYBDINPUT layout; INPUT is a 4-byte type + union) ──
    private const uint INPUT_KEYBOARD = 1;
    private const uint KEYEVENTF_UNICODE = 0x0004;
    private const uint KEYEVENTF_KEYUP = 0x0002;

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT
    {
        public uint type;
        public InputUnion U;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)] public KEYBDINPUT ki;
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

    [DllImport("user32.dll")]
    private static extern uint SendInput(uint nInputs,
        INPUT[] pInputs, int cbSize);
}

/// <summary>Insert through the existing terminal bridge (sendText semantics,
/// never an automatic Enter).</summary>
public static class TerminalBridgeInsert
{
    public static bool Write(string result)
    {
        try
        {
            using var pipe = new System.IO.Pipes.NamedPipeClientStream(
                ".", @"\\.\pipe\Toustovac.TerminalBridge.v1",
                System.IO.Pipes.PipeDirection.InOut);
            pipe.Connect(500);
            var message = new
            {
                protocol = "toustovac-terminal-bridge/2",
                kind = "insert_request",
                message_id = Guid.NewGuid().ToString("N")[..12],
                command = result,
                auto_execute = false,
            };
            var json = System.Text.Json.JsonSerializer.SerializeToElement(
                message);
            var frame = Protocol.Framing.Encode(json);
            pipe.Write(frame, 0, frame.Length);
            pipe.Flush();
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }
}
