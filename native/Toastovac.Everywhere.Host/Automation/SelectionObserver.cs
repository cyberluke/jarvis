// Windows UI Automation selection observer. Extracts selected text,
// geometry and identity into a SelectionSnapshot dictionary (the same wire
// shape the Python broker parses). Password controls are never read
// (§Security boundaries). Written against the WPF System.Windows.Automation
// surface (UIAutomationClient) that ships in Microsoft.WindowsDesktop.App.WPF.
using System.Windows.Automation;
using Toastovac.Everywhere.Host.Protocol;

namespace Toastovac.Everywhere.Host.Automation;

public sealed class SelectionObserver
{
    /// <summary>Read the current selection from the focused UIA element.
    /// Returns null when no readable selection exists (fail closed).</summary>
    public static Dictionary<string, object?>? CaptureFromFocus()
    {
        var element = AutomationElement.FocusedElement;
        if (element is null)
        {
            return null;
        }
        return Capture(element);
    }

    /// <summary>True when the element supports the given pattern, checking
    /// the supported set (there is no AutomationElement.PatternAvailable).</summary>
    private static bool Supports(AutomationElement element,
        AutomationPattern pattern)
    {
        try
        {
            foreach (var p in element.GetSupportedPatterns())
            {
                if (p.Id == pattern.Id)
                {
                    return true;
                }
            }
        }
        catch (Exception)
        {
            // Fail closed: pattern treated as unsupported.
        }
        return false;
    }

    public static Dictionary<string, object?>? Capture(AutomationElement element)
    {
        string text;
        try
        {
            if (Supports(element, TextPatternIdentifiers.Pattern))
            {
                var pattern = (TextPattern)element.GetCurrentPattern(
                    TextPatternIdentifiers.Pattern);
                var selection = pattern.GetSelection();
                var range = (selection is not null && selection.Length > 0)
                    ? selection[0]
                    : null;
                text = range?.GetText(-1) ?? string.Empty;
            }
            else if (Supports(element, ValuePatternIdentifiers.Pattern))
            {
                var value = (ValuePattern)element.GetCurrentPattern(
                    ValuePatternIdentifiers.Pattern);
                text = value.Current.Value ?? string.Empty;
            }
            else
            {
                text = element.Current.Name ?? string.Empty;
            }
        }
        catch (Exception)
        {
            return null;
        }

        var isPassword = element.Current.IsPassword;
        if (isPassword)
        {
            // Password controls are excluded by contract (§Security).
            return null;
        }

        var bounds = new List<double[]>();
        try
        {
            var rect = element.Current.BoundingRectangle;
            if (rect != System.Windows.Rect.Empty)
            {
                bounds.Add(new[]
                {
                    rect.X, rect.Y, rect.X + rect.Width, rect.Y + rect.Height,
                });
            }
        }
        catch (Exception)
        {
            // Geometry is optional metadata; identity still stands.
        }

        var hasValuePattern = Supports(element, ValuePatternIdentifiers.Pattern);
        var editable = hasValuePattern;
        if (hasValuePattern)
        {
            try
            {
                var value = (ValuePattern)element.GetCurrentPattern(
                    ValuePatternIdentifiers.Pattern);
                editable = !value.Current.IsReadOnly;
            }
            catch (Exception)
            {
                editable = false;
            }
        }

        var capability = editable
            ? ReplaceCapability.ValuePatternEdit
            : ReplaceCapability.CopyOnly;

        var processId = element.Current.ProcessId;
        var processName = ProcessNameOf(processId);

        var snapshot = new Dictionary<string, object?>
        {
            ["snapshot_id"] = $"u{Guid.NewGuid():N}"[..17],
            ["revision"] = 1,
            ["captured_at"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                / 1000.0,
            ["source_kind"] = SourceKinds.UiaText,
            ["provider_id"] = "uia",
            ["text"] = text,
            ["text_hash"] = TextHash(text),
            ["hwnd"] = element.Current.NativeWindowHandle,
            ["process_id"] = processId,
            ["process_name"] = processName,
            ["window_title"] = element.Current.Name ?? "",
            ["editable"] = editable,
            ["replace_capability"] = capability,
            ["selection_bounds"] = bounds,
            ["semantic_context"] = new Dictionary<string, object?>
            {
                ["control_type"] = element.Current.ControlType.Id,
            },
            ["provider_token"] = new Dictionary<string, object?>
            {
                ["automation_id"] = element.Current.AutomationId,
                ["control_type"] = element.Current.ControlType.Id,
                ["selected_text_hash"] = TextHash(text),
                ["is_password"] = isPassword,
            },
        };
        return snapshot;
    }

    /// <summary>Live identity for revalidation at Apply time.</summary>
    public static Dictionary<string, object?> CurrentState(
        AutomationElement? element)
    {
        if (element is null)
        {
            return new Dictionary<string, object?> { ["gone"] = true };
        }
        var captured = Capture(element) ?? new Dictionary<string, object?>();
        captured["gone"] = false;
        return captured;
    }

    private static string ProcessNameOf(int processId)
    {
        try
        {
            return System.Diagnostics.Process
                .GetProcessById(processId).ProcessName;
        }
        catch (Exception)
        {
            return "";
        }
    }

    private static string TextHash(string text)
    {
        using var sha = System.Security.Cryptography.SHA256.Create();
        var bytes = sha.ComputeHash(
            System.Text.Encoding.UTF8.GetBytes(text ?? ""));
        return Convert.ToHexString(bytes).ToLowerInvariant();
    }
}
