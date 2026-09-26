// AI Subtitles overlay: a bottom, full-width bar that shows live translated
// subtitles over whatever is playing (Chrome, YouTube, a video call).
// Controls: source language (auto-detect or specific), target language,
// live-audio (TTS) checkbox, and an Asian politeness/register dropdown for
// ko/ja/vi/zh. Transcript lines stream in from the Everywhere broker.
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Toastovac.Everywhere.Host.Pipe;

namespace Toastovac.Everywhere.Host.App;

public static class SubtitlesOverlay
{
    private static void LogSub(string line) => EverywhereApp.Log(line);

    private static Window? _window;
    private static StackPanel? _transcriptPanel;
    private static ComboBox? _srcCombo;
    private static ComboBox? _tgtCombo;
    private static ComboBox? _politenessCombo;
    private static CheckBox? _ttsCheck;
    private static Button? _castBtn;
    private static bool _casting;
    private static EverywherePipeClient? _pipe;
    private static bool _running;
    private static System.Threading.Timer? _pollTimer;
    private static Microsoft.UI.Dispatching.DispatcherQueue? _dq;
    private static StackPanel? _castMenu;
    private static ComboBox? _castDeviceCombo;
    private static ComboBox? _castModeCombo;
    private static TextBox? _castUrlInput;
    private static Button? _castStartBtn;
    private static Button? _castStopBtn;
    private static TextBlock? _castStatusText;

    /// <summary>Toggle the subtitles overlay on/off.</summary>
    public static void Toggle(Window? anchor, EverywherePipeClient pipe)
    {
        _pipe = pipe;
        if (_window is not null)
        {
            Close();
            return;
        }
        _dq = Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();
        var win = new Window { Title = "Toustovač Subtitles" };
        _window = win;
        win.Content = BuildContent();
        Windowing.Chrome.MakeToolOverlay(win);
        PositionBottom(win);
        win.Activate();
        LoadOptions();
    }

    private static UIElement BuildContent()
    {
        var root = new StackPanel { Padding = new Thickness(20, 14, 20, 16) };
        // Rounded card styling: the bar reads as a soft floating panel, not a
        // harsh square edge.
        root.CornerRadius = new CornerRadius(16);
        root.Background = new SolidColorBrush(
            Windows.UI.Color.FromArgb(245, 20, 20, 24));

        // Control row: source -> target, politeness, live-audio, close.
        var controls = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 14,
        };
        _srcCombo = new ComboBox { MinWidth = 200, FontSize = 16,
            PlaceholderText = "From: Auto" };
        _tgtCombo = new ComboBox { MinWidth = 200, FontSize = 16,
            PlaceholderText = "To: Czech" };
        _politenessCombo = new ComboBox { MinWidth = 240, FontSize = 16,
            PlaceholderText = "Register (Asian)", Visibility = Visibility.Collapsed };
        _ttsCheck = new CheckBox { Content = "🔊 Live audio", FontSize = 16 };
        _ttsCheck.IsEnabled = false;  // enabled only when the target has a voice
        var startBtn = new Button { Content = "▶ Start", FontSize = 16,
            MinHeight = 44 };
        startBtn.Click += (_, _) => Start();
        var stopBtn = new Button { Content = "■ Stop", FontSize = 16,
            MinHeight = 44 };
        stopBtn.Click += (_, _) => Stop();
        _castBtn = new Button { Content = "📺 Cast", FontSize = 16,
            MinHeight = 44 };
        _castBtn.Click += (_, _) => ToggleCastMenu();
        var closeBtn = new Button { Content = "✕", FontSize = 16,
            MinHeight = 44 };
        closeBtn.Click += (_, _) => Close();

        controls.Children.Add(new TextBlock { Text = "Subtitles",
            FontSize = 20, VerticalAlignment = VerticalAlignment.Center });
        controls.Children.Add(_srcCombo);
        controls.Children.Add(new TextBlock { Text = "→", FontSize = 20,
            VerticalAlignment = VerticalAlignment.Center });
        controls.Children.Add(_tgtCombo);
        controls.Children.Add(_politenessCombo);
        controls.Children.Add(_ttsCheck);
        controls.Children.Add(startBtn);
        controls.Children.Add(stopBtn);
        controls.Children.Add(_castBtn);
        controls.Children.Add(closeBtn);
        root.Children.Add(controls);

        // Cast menu row (hidden by default, expands on Cast button click).
        _castMenu = new StackPanel
        {
            Orientation = Orientation.Vertical,
            Spacing = 8,
            Visibility = Visibility.Collapsed,
            Margin = new Thickness(0, 10, 0, 0),
        };
        BuildCastMenu();
        root.Children.Add(_castMenu);

        // Transcript area (scrolling).
        var scroller = new ScrollViewer { MaxHeight = 220 };
        _transcriptPanel = new StackPanel { Spacing = 4 };
        scroller.Content = _transcriptPanel;
        root.Children.Add(scroller);
        return root;
    }

    private static void PositionBottom(Window win)
    {
        // Bottom bar, full physical screen width, docked to the bottom of the
        // work area. PrimaryScreenPhysical uses GetSystemMetrics (physical
        // pixels regardless of DPI virtualization), so the bar spans the whole
        // screen on a 4K/200% display.
        var (wx, wy, ww, wh) = Windowing.MonitorHelper.PrimaryScreenPhysical();
        var height = Math.Min(wh * 0.26, wh - 100);
        win.AppWindow.Resize(new Windows.Graphics.SizeInt32(
            (int)ww, (int)height));
        win.AppWindow.Move(new Windows.Graphics.PointInt32(
            (int)wx, (int)(wy + wh - height)));
    }

    private static void LoadOptions()
    {
        if (_pipe is null) return;
        var reply = _pipe.RoundTrip(BuildCmd("options"));
        LogSub($"options reply: {(reply is null ? "null" : "ok")}");
        if (reply is null) return;
        if (reply.Value.TryGetProperty("kind", out var kind))
        {
            LogSub($"options kind: {kind.GetString()}");
        }
        if (!reply.Value.TryGetProperty("sources", out var sources))
        {
            LogSub("options: no sources field");
            return;
        }

        foreach (var s in sources.EnumerateArray())
        {
            var code = s.GetProperty("code").GetString() ?? "";
            var label = s.TryGetProperty("name_native", out var nn)
                ? $"{nn.GetString()} ({code})" : code;
            _srcCombo?.Items.Add(new ComboBoxItem { Content = label, Tag = code });
        }
        if (_srcCombo is not null && _srcCombo.Items.Count > 0)
            _srcCombo.SelectedIndex = 0;

        if (reply.Value.TryGetProperty("targets", out var targets))
        {
            foreach (var t in targets.EnumerateArray())
            {
                var code = t.GetProperty("code").GetString() ?? "";
                var native = t.TryGetProperty("name_native", out var nn)
                    ? nn.GetString() : code;
                var tts = t.TryGetProperty("tts", out var tv) && tv.GetBoolean();
                // Badge: a speaker glyph marks a Piper-supported voice.
                var label = $"{native} ({code})" + (tts ? "  🔊" : "");
                _tgtCombo?.Items.Add(new ComboBoxItem { Content = label, Tag = code });
            }
        }
        if (_tgtCombo is not null)
        {
            // Default the target to Czech.
            for (var i = 0; i < _tgtCombo.Items.Count; i++)
            {
                if (((ComboBoxItem)_tgtCombo.Items[i]).Tag as string == "cs")
                {
                    _tgtCombo.SelectedIndex = i;
                    break;
                }
            }
        }
        _tgtCombo?.SelectionChanged += (_, _) => OnTargetChanged();
        OnTargetChanged();
    }

    private static void OnTargetChanged()
    {
        if (_tgtCombo?.SelectedItem is not ComboBoxItem item) return;
        var code = item.Tag as string ?? "cs";
        var label = item.Content?.ToString() ?? "";
        var tts = label.Contains("🔊");
        // Live-audio checkbox only when Piper has a voice for the target.
        if (_ttsCheck is not null) _ttsCheck.IsEnabled = tts;
        if (!tts && _ttsCheck is not null) _ttsCheck.IsChecked = false;
        // Politeness dropdown only for Asian register languages.
        var hasPoliteness = code is "ko" or "ja" or "vi" or "zh";
        if (_politenessCombo is not null)
        {
            _politenessCombo.Visibility = hasPoliteness
                ? Visibility.Visible : Visibility.Collapsed;
            if (hasPoliteness) LoadPoliteness(code);
        }
    }

    private static void LoadPoliteness(string language)
    {
        if (_pipe is null || _politenessCombo is null) return;
        var reply = _pipe.RoundTrip(BuildCmd("options"));
        if (reply is null) return;
        if (!reply.Value.TryGetProperty("politeness", out var all)) return;
        if (!all.TryGetProperty(language, out var entries)) return;
        _politenessCombo.Items.Clear();
        _politenessCombo.Items.Add(new ComboBoxItem
        { Content = "Neutral / default", Tag = "" });
        foreach (var e in entries.EnumerateArray())
        {
            var native = e.GetProperty("native").GetString() ?? "";
            var en = e.GetProperty("en").GetString() ?? "";
            var key = e.GetProperty("key").GetString() ?? "";
            _politenessCombo.Items.Add(new ComboBoxItem
            { Content = $"{native} — {en}", Tag = key });
        }
        _politenessCombo.SelectedIndex = 0;
    }

    private static void Start()
    {
        if (_pipe is null) return;
        var source = (_srcCombo?.SelectedItem as ComboBoxItem)?.Tag as string
            ?? "auto";
        var target = (_tgtCombo?.SelectedItem as ComboBoxItem)?.Tag as string
            ?? "cs";
        var politeness = (_politenessCombo?.SelectedItem as ComboBoxItem)
            ?.Tag as string ?? "";
        var liveAudio = _ttsCheck?.IsChecked == true;
        var reply = _pipe.RoundTrip(BuildCmd("start",
            ("source", source), ("target", target),
            ("live_audio", liveAudio), ("politeness", politeness)));
        if (reply is not null
            && reply.Value.TryGetProperty("kind", out var k)
            && k.GetString() == "subtitles_started")
        {
            _running = true;
            _pollTimer = new System.Threading.Timer(_ => Poll(),
                null, 300, 300);
        }
    }

    private static void Stop()
    {
        _running = false;
        _pollTimer?.Dispose();
        _pollTimer = null;
        _pipe?.RoundTrip(BuildCmd("stop"));
    }

    private static void ToggleCast()
    {
        if (_pipe is null) return;
        _casting = !_casting;
        var reply = _pipe.RoundTrip(BuildCmd("cast", ("enable", _casting)));
        if (reply is not null
            && reply.Value.TryGetProperty("cast_url", out var url))
        {
            var u = url.GetString() ?? "";
            if (_castBtn is not null)
            {
                _castBtn.Content = _casting && u.Length > 0
                    ? $"📺 Casting ({u})" : "📺 Cast";
            }
            EverywhereApp.Log($"subtitle cast {(u.Length > 0 ? u : "off")}");
        }
    }

    private static void ToggleCastMenu()
    {
        if (_castMenu is null) return;
        _castMenu.Visibility = _castMenu.Visibility == Visibility.Visible
            ? Visibility.Collapsed : Visibility.Visible;
        if (_castMenu.Visibility == Visibility.Visible)
        {
            LoadCastDevices();
        }
    }

    private static void BuildCastMenu()
    {
        if (_castMenu is null) return;

        // Device selection row
        var deviceRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        deviceRow.Children.Add(new TextBlock { Text = "Device:", FontSize = 14,
            VerticalAlignment = VerticalAlignment.Center, MinWidth = 60 });
        _castDeviceCombo = new ComboBox { MinWidth = 200, FontSize = 14,
            PlaceholderText = "Select device..." };
        deviceRow.Children.Add(_castDeviceCombo);
        var refreshBtn = new Button { Content = "🔄", FontSize = 14, MinHeight = 32 };
        refreshBtn.Click += (_, _) => LoadCastDevices();
        deviceRow.Children.Add(refreshBtn);
        _castMenu.Children.Add(deviceRow);

        // Mode selection row
        var modeRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        modeRow.Children.Add(new TextBlock { Text = "Mode:", FontSize = 14,
            VerticalAlignment = VerticalAlignment.Center, MinWidth = 60 });
        _castModeCombo = new ComboBox { MinWidth = 200, FontSize = 14 };
        _castModeCombo.Items.Add(new ComboBoxItem { Content = "📺 LAN Subtitles Only", Tag = "lan_subtitles" });
        _castModeCombo.Items.Add(new ComboBoxItem { Content = "🎬 Media URL", Tag = "media_url" });
        _castModeCombo.Items.Add(new ComboBoxItem { Content = "📁 Local File", Tag = "local_file" });
        _castModeCombo.Items.Add(new ComboBoxItem { Content = "🖥️ Screen Mirror", Tag = "screen_mirror" });
        _castModeCombo.Items.Add(new ComboBoxItem { Content = "📑 Browser Tab", Tag = "browser_tab" });
        _castModeCombo.Items.Add(new ComboBoxItem { Content = "▶️ YouTube", Tag = "youtube" });
        _castModeCombo.SelectedIndex = 0;
        _castModeCombo.SelectionChanged += (_, _) => OnCastModeChanged();
        modeRow.Children.Add(_castModeCombo);
        _castMenu.Children.Add(modeRow);

        // URL input row (for media_url / youtube modes)
        var urlRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        urlRow.Children.Add(new TextBlock { Text = "URL:", FontSize = 14,
            VerticalAlignment = VerticalAlignment.Center, MinWidth = 60 });
        _castUrlInput = new TextBox { MinWidth = 300, FontSize = 14,
            PlaceholderText = "https://..." };
        urlRow.Children.Add(_castUrlInput);
        _castMenu.Children.Add(urlRow);

        // Control buttons row
        var btnRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        _castStartBtn = new Button { Content = "▶ Start Cast", FontSize = 14, MinHeight = 36 };
        _castStartBtn.Click += (_, _) => StartCastSession();
        btnRow.Children.Add(_castStartBtn);
        _castStopBtn = new Button { Content = "■ Stop", FontSize = 14, MinHeight = 36 };
        _castStopBtn.Click += (_, _) => StopCastSession();
        btnRow.Children.Add(_castStopBtn);
        _castMenu.Children.Add(btnRow);

        // Status text
        _castStatusText = new TextBlock { FontSize = 12, Opacity = 0.7,
            TextWrapping = TextWrapping.Wrap };
        _castMenu.Children.Add(_castStatusText);
    }

    private static void OnCastModeChanged()
    {
        if (_castModeCombo?.SelectedItem is not ComboBoxItem item) return;
        var mode = item.Tag as string ?? "lan_subtitles";
        // Show URL input only for modes that need it
        var needsUrl = mode is "media_url" or "youtube";
        if (_castUrlInput is not null)
        {
            _castUrlInput.Visibility = needsUrl ? Visibility.Visible : Visibility.Collapsed;
        }
    }

    private static void LoadCastDevices()
    {
        if (_pipe is null || _castDeviceCombo is null) return;
        _castDeviceCombo.Items.Clear();
        var reply = _pipe.RoundTrip(BuildCmd("cast_devices"));
        if (reply is null) return;
        if (!reply.Value.TryGetProperty("devices", out var devices)) return;
        foreach (var d in devices.EnumerateArray())
        {
            var name = d.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
            var model = d.TryGetProperty("model", out var m) ? m.GetString() ?? "" : "";
            _castDeviceCombo.Items.Add(new ComboBoxItem
            {
                Content = $"{name} ({model})",
                Tag = name
            });
        }
        if (_castDeviceCombo.Items.Count > 0)
            _castDeviceCombo.SelectedIndex = 0;
    }

    private static void StartCastSession()
    {
        if (_pipe is null) return;
        var mode = (_castModeCombo?.SelectedItem as ComboBoxItem)?.Tag as string ?? "lan_subtitles";
        var device = (_castDeviceCombo?.SelectedItem as ComboBoxItem)?.Tag as string ?? "";
        var url = _castUrlInput?.Text ?? "";

        var fields = new System.Collections.Generic.List<(string, object)>
        {
            ("mode", mode),
            ("device", device),
        };
        if (mode == "media_url" || mode == "youtube")
            fields.Add(("url", url));
        if (mode == "local_file")
            fields.Add(("path", url));

        var reply = _pipe.RoundTrip(BuildCmd("cast_start", fields.ToArray()));
        if (reply is not null && reply.Value.TryGetProperty("active", out var active) && active.GetBoolean())
        {
            if (_castStatusText is not null)
                _castStatusText.Text = "Cast session started";
            EverywhereApp.Log("cast session started");
        }
        else
        {
            if (_castStatusText is not null)
                _castStatusText.Text = "Failed to start cast session";
        }
    }

    private static void StopCastSession()
    {
        if (_pipe is null) return;
        _pipe.RoundTrip(BuildCmd("cast_stop"));
        if (_castStatusText is not null)
            _castStatusText.Text = "Cast session stopped";
    }

    private static void Poll()
    {
        if (!_running || _pipe is null) return;
        var reply = _pipe.RoundTrip(BuildCmd("poll"));
        if (reply is null) return;
        if (!reply.Value.TryGetProperty("lines", out var lines)) return;
        if (!reply.Value.TryGetProperty("running", out var run)
            || !run.GetBoolean())
        {
            _running = false;
            return;
        }
        foreach (var line in lines.EnumerateArray())
        {
            var src = line.TryGetProperty("source_text", out var st)
                ? st.GetString() ?? "" : "";
            var tgt = line.TryGetProperty("translated_text", out var tt)
                ? tt.GetString() ?? "" : "";
            AppendLine(tgt.Length > 0 ? tgt : src);
        }
    }

    private static void AppendLine(string text)
    {
        _dq?.TryEnqueue(() =>
        {
            if (_transcriptPanel is null) return;
            _transcriptPanel.Children.Add(new TextBlock
            {
                Text = text,
                FontSize = 22,
                TextWrapping = TextWrapping.Wrap,
            });
            // Keep the last ~20 lines so the bar stays readable.
            while (_transcriptPanel.Children.Count > 20)
            {
                _transcriptPanel.Children.RemoveAt(0);
            }
        });
    }

    private static void Close()
    {
        Stop();
        var win = _window;
        _window = null;
        if (win is not null)
        {
            try { win.AppWindow.Hide(); } catch (Exception) { }
            try { win.Close(); } catch (Exception) { }
        }
    }

    private static JsonElement BuildCmd(string command,
        params (string key, object value)[] fields)
    {
        var cmd = new Dictionary<string, object?>
        {
            ["protocol"] = Protocol.Framing.ProtocolId,
            ["kind"] = "subtitles",
            ["request_id"] = Guid.NewGuid().ToString("N")[..12],
            ["command"] = command,
        };
        foreach (var (key, value) in fields)
        {
            cmd[key] = value;
        }
        return JsonSerializer.SerializeToElement(cmd);
    }
}
