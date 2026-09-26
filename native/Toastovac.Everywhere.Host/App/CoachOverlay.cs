// Interview / Meeting Coach overlay: a bottom bar showing the live dual-lane
// transcript (me = mic, others = the remote meeting) plus an AI insight panel
// that surfaces a context-aware answer hint whenever the interviewer asks a
// question. Answer-language dropdown (English/Czech) and a domain hint.
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Toastovac.Everywhere.Host.Pipe;

namespace Toastovac.Everywhere.Host.App;

public static class CoachOverlay
{
    private static Window? _window;
    private static StackPanel? _feed;
    private static ComboBox? _langCombo;
    private static ComboBox? _domainCombo;
    private static EverywherePipeClient? _pipe;
    private static bool _running;
    private static System.Threading.Timer? _pollTimer;
    private static Microsoft.UI.Dispatching.DispatcherQueue? _dq;

    public static void Toggle(Window? anchor, EverywherePipeClient pipe)
    {
        _pipe = pipe;
        if (_window is not null)
        {
            Close();
            return;
        }
        // Create the window on the toolbar window's UI thread (the hook fires
        // on a background thread; a Window must be created on the UI thread or
        // the process dies on first render).
        var dq = anchor?.DispatcherQueue
            ?? Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();
        if (dq is null)
        {
            EverywhereApp.Log("coach overlay: no dispatcher queue");
            return;
        }
        dq.TryEnqueue(() => CreateWindow());
    }

    private static void CreateWindow()
    {
        try
        {
            _dq = Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();
            var win = new Window { Title = "Toustovač Coach" };
            _window = win;
            win.Content = BuildContent();
            Windowing.Chrome.MakeToolOverlay(win);
            Position(win);
            win.Activate();
            EverywhereApp.Log("coach overlay shown");
        }
        catch (Exception ex)
        {
            EverywhereApp.Log($"coach overlay failed: {ex.GetType().Name}: "
                + ex.Message);
            _window = null;
        }
    }

    private static UIElement BuildContent()
    {
        // Rounded, soft dark card — no harsh square edges.
        var root = new StackPanel { Padding = new Thickness(20, 14, 20, 16) };
        root.CornerRadius = new CornerRadius(16);
        root.Background = new SolidColorBrush(
            Windows.UI.Color.FromArgb(245, 20, 20, 24));

        // A wrap panel so the controls never truncate on a narrow logical
        // width (the window is physical-pixel sized; at 200% DPI the content
        // DIP width is half). Buttons stay reachable by wrapping to a new row.
        var controls = new StackPanel
        { Orientation = Orientation.Horizontal, Spacing = 10 };
        controls.Children.Add(new TextBlock { Text = "🎓 Coach", FontSize = 18,
            VerticalAlignment = VerticalAlignment.Center });
        // Answer language: the coach hints in this language. Broad coverage so
        // interviews in any of these work.
        _langCombo = new ComboBox { MinWidth = 150, FontSize = 15 };
        var langs = new (string label, string code)[]
        {
            ("English", "en"), ("Čeština", "cs"), ("中文", "zh"),
            ("Tiếng Việt", "vi"), ("한국어", "ko"), ("Deutsch", "de"),
            ("Español", "es"), ("Polski", "pl"), ("Nederlands", "nl"),
            ("Français", "fr"), ("Slovenčina", "sk"), ("العربية", "ar"),
        };
        foreach (var (label, code) in langs)
        {
            _langCombo.Items.Add(new ComboBoxItem { Content = label, Tag = code });
        }
        _langCombo.SelectedIndex = 0;
        controls.Children.Add(_langCombo);
        // Coach role/domain: interview presets plus a general chit-chat coach
        // that analyses the other person's thinking in casual conversation.
        _domainCombo = new ComboBox { MinWidth = 320, FontSize = 14 };
        _domainCombo.Items.Add(new ComboBoxItem
        { Content = "💼 AI/ML developer interview", Tag = "AI/ML developer interview (Python, ML, HR)" });
        _domainCombo.Items.Add(new ComboBoxItem
        { Content = "💻 General technical interview", Tag = "software engineering technical interview" });
        _domainCombo.Items.Add(new ComboBoxItem
        { Content = "🤝 HR / behavioral interview", Tag = "HR behavioral interview" });
        _domainCombo.Items.Add(new ComboBoxItem
        { Content = "💬 Chit-chat / thinking coach", Tag = "__chitchat__" });
        _domainCombo.SelectedIndex = 0;
        controls.Children.Add(_domainCombo);
        var startBtn = new Button { Content = "▶ Start", FontSize = 14, MinHeight = 40 };
        startBtn.Click += (_, _) => Start();
        var stopBtn = new Button { Content = "■ Stop", FontSize = 14, MinHeight = 40 };
        stopBtn.Click += (_, _) => Stop();
        var sumBtn = new Button { Content = "📝 Summary", FontSize = 14, MinHeight = 40 };
        sumBtn.Click += (_, _) => Summarize();
        var closeBtn = new Button { Content = "✕", FontSize = 14, MinHeight = 40 };
        closeBtn.Click += (_, _) => Close();
        controls.Children.Add(startBtn);
        controls.Children.Add(stopBtn);
        controls.Children.Add(sumBtn);
        controls.Children.Add(closeBtn);
        root.Children.Add(controls);

        var scroller = new ScrollViewer { MaxHeight = 320 };
        _feed = new StackPanel { Spacing = 6 };
        scroller.Content = _feed;
        root.Children.Add(scroller);
        return root;
    }

    private static void Position(Window win)
    {
        // Full physical screen width, docked to the bottom of the work area.
        // PrimaryScreenPhysical uses GetSystemMetrics, which reports physical
        // pixels even when the process is DPI-virtualized, so the bar spans
        // the whole screen on a 4K/200% display.
        var (wx, wy, ww, wh) = Windowing.MonitorHelper.PrimaryScreenPhysical();
        var height = Math.Min(wh * 0.32, wh - 100);
        win.AppWindow.Resize(new Windows.Graphics.SizeInt32((int)ww,
            (int)height));
        win.AppWindow.Move(new Windows.Graphics.PointInt32((int)wx,
            (int)(wy + wh - height)));
    }

    private static void Start()
    {
        if (_pipe is null) return;
        var lang = (_langCombo?.SelectedItem as ComboBoxItem)?.Tag as string ?? "en";
        var domain = (_domainCombo?.SelectedItem as ComboBoxItem)?.Tag as string ?? "";
        var reply = _pipe.RoundTrip(BuildCmd("start",
            ("answer_language", lang), ("domain_hint", domain)));
        if (reply is not null
            && reply.Value.TryGetProperty("kind", out var k)
            && k.GetString() == "coach_started")
        {
            _running = true;
            _pollTimer = new System.Threading.Timer(_ => Poll(), null, 300, 300);
        }
    }

    private static void Stop()
    {
        _running = false;
        _pollTimer?.Dispose();
        _pollTimer = null;
        _pipe?.RoundTrip(BuildCmd("stop"));
    }

    private static void Summarize()
    {
        if (_pipe is null) return;
        var reply = _pipe.RoundTrip(BuildCmd("summarize"));
        if (reply is null) return;
        if (reply.Value.TryGetProperty("summary", out var s))
        {
            var text = s.GetString() ?? "";
            if (text.Length > 0)
            {
                AppendBlock("📝 Meeting summary", text, isHint: true);
            }
        }
    }

    private static void Poll()
    {
        if (!_running || _pipe is null) return;
        var reply = _pipe.RoundTrip(BuildCmd("poll"));
        if (reply is null) return;
        if (!reply.Value.TryGetProperty("events", out var events)) return;
        if (!reply.Value.TryGetProperty("running", out var run)
            || !run.GetBoolean())
        {
            _running = false;
            return;
        }
        foreach (var ev in events.EnumerateArray())
        {
            var type = ev.TryGetProperty("type", out var t)
                ? t.GetString() ?? "" : "";
            if (type == "transcript")
            {
                var speaker = ev.GetProperty("speaker").GetString() ?? "";
                var text = ev.GetProperty("text").GetString() ?? "";
                AppendBlock(speaker == "me" ? "🎤 You" : "🗣 Them", text,
                    isHint: false);
            }
            else if (type == "hint")
            {
                var q = ev.GetProperty("question").GetString() ?? "";
                var a = ev.GetProperty("answer").GetString() ?? "";
                AppendBlock($"💡 Hint — {q}", a, isHint: true);
            }
        }
    }

    private static void AppendBlock(string header, string body, bool isHint)
    {
        _dq?.TryEnqueue(() =>
        {
            if (_feed is null) return;
            var card = new StackPanel
            {
                Spacing = 2,
                Padding = new Thickness(10, 6, 10, 6),
            };
            if (isHint)
            {
                card.Background = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(40, 245, 158, 11));
            }
            card.Children.Add(new TextBlock
            {
                Text = header,
                FontSize = 14,
                Opacity = 0.8,
            });
            card.Children.Add(new TextBlock
            {
                Text = body,
                FontSize = 19,
                TextWrapping = TextWrapping.Wrap,
            });
            _feed.Children.Add(card);
            while (_feed.Children.Count > 40)
            {
                _feed.Children.RemoveAt(0);
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
            ["kind"] = "coach",
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
