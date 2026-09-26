// Host-side config reader: reads the same flat keys from config.json the
// Python loader uses, so the host and the broker always agree. Defaults
// mirror the Python EVERYWHERE_DEFAULT_HOTKEYS table.
using System.Text.Json;

namespace Toastovac.Everywhere.Host;

public static class Config
{
    public static Dictionary<string, string> LoadHotkeyTable()
    {
        var table = new Dictionary<string, string>
        {
            ["toolbar"] = "ctrl+shift+space",
            ["rewrite"] = "ctrl+shift+c",
            ["proofread"] = "ctrl+shift+h",
            ["alternatives"] = "ctrl+shift+l",
            ["explain"] = "ctrl+shift+x",
            ["translate"] = "ctrl+shift+t",
            ["prompt"] = "ctrl+shift+j",
            ["ocr"] = "alt+drag",
            ["help"] = "ctrl+/",
            ["cancel"] = "esc",
        };
        foreach (var slot in new[]
        {
            "toolbar", "rewrite", "proofread", "alternatives", "explain",
            "translate", "prompt", "ocr", "help", "cancel",
        })
        {
            var value = ReadString($"everywhere_hotkey_{slot}");
            if (!string.IsNullOrWhiteSpace(value))
            {
                table[slot] = value.Trim().ToLowerInvariant();
            }
        }
        return table;
    }

    public static string OcrBackend() =>
        ReadString("everywhere_ocr_backend") ?? "windows-ai";

    public static bool EverywhereEnabled()
    {
        var value = ReadString("everywhere_enabled");
        return value is null or "true" or "True" or "1";
    }

    private static string? ReadString(string key)
    {
        try
        {
            var path = ConfigPath();
            if (!File.Exists(path))
            {
                return null;
            }
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.TryGetProperty(key, out var v)
                && v.ValueKind == JsonValueKind.String)
            {
                return v.GetString();
            }
            if (v.ValueKind == JsonValueKind.True) return "true";
            if (v.ValueKind == JsonValueKind.False) return "false";
        }
        catch (Exception)
        {
        }
        return null;
    }

    public static string ConfigPath()
    {
        var overridePath = Environment.GetEnvironmentVariable(
            "JARVIS_CONFIG_PATH");
        if (!string.IsNullOrWhiteSpace(overridePath))
        {
            return overridePath;
        }
        var xdg = Environment.GetEnvironmentVariable("XDG_CONFIG_HOME");
        var home = string.IsNullOrWhiteSpace(xdg)
            ? Path.Combine(
                Environment.GetFolderPath(
                    Environment.SpecialFolder.UserProfile), ".config")
            : xdg;
        return Path.Combine(home, "jarvis", "config.json");
    }
}
