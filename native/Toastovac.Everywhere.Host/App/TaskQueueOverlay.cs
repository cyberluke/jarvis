// The Everywhere queue overlay: the unobtrusive delivery surface for async
// tasks. One compact window shows every queued/running/finished action; the
// user can keep working while models run, dismiss the toolbar and find the
// result here later. Finished tasks are clickable (opens the result panel),
// running tasks get a Cancel button, the window is draggable by its header
// and collapsible to a single pill. Position persists across restarts.
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Toastovac.Everywhere.Host.Protocol;
using Toastovac.Everywhere.Host.Windowing;
using Windows.Foundation;

namespace Toastovac.Everywhere.Host.App;

/// <summary>One row's worth of task state (mirrors the broker's task_event).</summary>
public sealed class TaskEntry
{
    public string TaskId { get; set; } = "";
    public string RequestId { get; set; } = "";
    public string Action { get; set; } = "";
    public string SnapshotId { get; set; } = "";
    public string State { get; set; } = "queued";   // queued|running|streaming|completed|failed|cancelled
    public string? Model { get; set; }
    public string? Profile { get; set; }
    public string TargetLanguage { get; set; } = "";
    public string Result { get; set; } = "";
    public string Failure { get; set; } = "";
    public bool CacheHit { get; set; }
    public bool Truncated { get; set; }
    public int RetryCount { get; set; }
    public double QueuedAt { get; set; }
    public double? StartedAt { get; set; }
    public double? FirstTokenAt { get; set; }
    public double? FinishedAt { get; set; }
    public int? DurationMs { get; set; }
    public int? FirstTokenMs { get; set; }
    public string SourceKind { get; set; } = "";
    public string ProcessName { get; set; } = "";
    public string Preview { get; set; } = "";
    public int ResultLength { get; set; }
    public int Position { get; set; }
    // Host-side extras (not part of the broker wire format): the replace
    // capability and the frozen snapshot this task was queued against, used
    // by Insert and by the toolbar-pending delivery path.
    public string Capability { get; set; } = ReplaceCapability.CopyOnly;
    public Dictionary<string, object?>? SnapshotDict { get; set; }

    public string ActionLabel => Action switch
    {
        "translate" => "Translate",
        "rewrite" => "Rewrite",
        "proofread" => "Proofread",
        "alternatives" => "Alternatives",
        "explain" => "Explain",
        "prompt" => "Prompt",
        "fix_command" => "Fix command",
        "explain_command" => "Explain command",
        "safer_variant" => "Safer variant",
        "docker_help" => "Docker help",
        "devops_help" => "DevOps help",
        _ => Action,
    };

    public bool IsFinished => State is "completed" or "failed" or "cancelled";
    public bool IsActive => State is "queued" or "running" or "streaming";
}

public static class TaskQueueOverlay
{
    private static Window? _window;
    private static Window? _anchor;
    private static StackPanel? _rows;
    private static TextBlock? _title;
    private static Button? _collapseBtn;
    private static Border? _toast;
    private static TextBlock? _toastText;
    private static readonly Dictionary<string, (Border row, TaskEntry task)>
        _rowsByTask = new();
    private static Action<string>? _onOpenTask;   // open result panel
    private static Action<string>? _onCancelTask;
    private static Action? _onClearFinished;
    private static bool _collapsed;
    private static bool _dragging;
    private static Point _dragStart;
    private static Windows.Graphics.PointInt32 _winStart;

    private static readonly string UiStatePath = System.IO.Path.Combine(
        System.Environment.GetFolderPath(
            System.Environment.SpecialFolder.LocalApplicationData),
        "Jarvis", "everywhere_queue_ui.json");

    private static Microsoft.UI.Dispatching.DispatcherQueueTimer? _toastTimer;

    public static bool IsOpen => _window is not null;

    /// <summary>Set the anchor window WITHOUT showing anything. The overlay
    /// is lazy: it appears only when the first task activity arrives (or a
    /// subscribed snapshot contains running tasks) — never empty at startup.</summary>
    public static void Register(Window anchor) => _anchor = anchor;

    public static void Init(Action<string> onOpenTask,
        Action<string> onCancelTask, Action onClearFinished)
    {
        _onOpenTask = onOpenTask;
        _onCancelTask = onCancelTask;
        _onClearFinished = onClearFinished;
    }

    public static void Show(Window anchor)
    {
        _anchor = anchor;
        if (_window is not null)
        {
            _window.AppWindow.Show();
            return;
        }
        var win = new Window { Title = "Toustovač Everywhere Queue" };
        _window = win;
        win.Closed += (_, _) =>
        {
            _window = null;
            _rowsByTask.Clear();
        };
        var root = BuildContent();
        win.Content = root;
        Chrome.MakeToolOverlay(win);
        var pos = LoadPosition();
        var (sx, sy, sw, sh) = MonitorHelper.PrimaryScreenPhysical();
        var x = pos?.x ?? (sx + sw - 380);
        var y = pos?.y ?? (sy + sh - 240);
        win.AppWindow.Resize(new Windows.Graphics.SizeInt32(360, 220));
        win.AppWindow.Move(new Windows.Graphics.PointInt32((int)x, (int)y));
        win.AppWindow.Show();
        if (_collapsed)
        {
            SetCollapsed(true);
        }
        EverywhereApp.Log("queue overlay shown");
    }

    public static void Hide()
    {
        var win = _window;
        _window = null;
        if (win is not null)
        {
            try { win.AppWindow.Hide(); } catch (Exception) { }
            try { win.Close(); } catch (Exception) { }
        }
        _rowsByTask.Clear();
        EverywhereApp.Log("queue overlay hidden");
    }

    /// <summary>Re-show the queue overlay after a Dismiss when new task
    /// activity arrives. Returns true when the window was (re)created; the
    /// caller repopulates the rows in that case.</summary>
    public static bool EnsureShown()
    {
        if (_window is not null) return false;
        if (_anchor is null) return false;
        Show(_anchor);
        return true;
    }

    public static void AddOrUpdate(TaskEntry task)
    {
        var dq = _window?.DispatcherQueue;
        if (dq is null)
        {
            // Keep the entry cached in the app; UI renders on next Show.
            return;
        }
        dq.TryEnqueue(() =>
        {
            if (_rows is null) return;
            if (_rowsByTask.TryGetValue(task.TaskId, out var existing))
            {
                existing.task.State = task.State;
                existing.task.Result = task.Result;
                existing.task.Failure = task.Failure;
                existing.task.DurationMs = task.DurationMs;
                existing.task.FirstTokenMs = task.FirstTokenMs;
                existing.task.Preview = task.Preview;
                existing.task.CacheHit = task.CacheHit;
                existing.task.RetryCount = task.RetryCount;
                existing.task.Position = task.Position;
                RenderRow(existing.row, existing.task);
            }
            else
            {
                var (row, _) = BuildRow(task);
                _rows.Children.Insert(0, row);
                _rowsByTask[task.TaskId] = (row, task);
            }
            UpdateTitle();
        });
    }

    public static void Remove(string taskId)
    {
        var dq = _window?.DispatcherQueue;
        if (dq is null) return;
        dq.TryEnqueue(() =>
        {
            if (_rowsByTask.TryGetValue(taskId, out var existing))
            {
                _rows?.Children.Remove(existing.row);
                _rowsByTask.Remove(taskId);
            }
            UpdateTitle();
        });
    }

    /// <summary>Transient notification: brief toast + expand + flash. Also
    /// re-shows a dismissed overlay — a finished/failed task is an event the
    /// user must be able to notice.</summary>
    public static void Notify(string taskId, string message)
    {
        EnsureShown();
        var dq = _window?.DispatcherQueue;
        if (dq is null) return;
        dq.TryEnqueue(() =>
        {
            if (_collapsed)
            {
                SetCollapsed(false);
            }
            if (_toast is not null && _toastText is not null)
            {
                _toastText.Text = message;
                _toast.Visibility = Visibility.Visible;
                _toast.Opacity = 1.0;
            }
            if (_rowsByTask.TryGetValue(taskId, out var existing))
            {
                Flash(existing.row);
            }
            EnsureToastTimer();
        });
    }

    public static void ReplaceAll(IEnumerable<TaskEntry> tasks)
    {
        var dq = _window?.DispatcherQueue;
        if (dq is null) return;
        dq.TryEnqueue(() =>
        {
            if (_rows is null) return;
            _rows.Children.Clear();
            _rowsByTask.Clear();
            foreach (var task in tasks)
            {
                var (row, entry) = BuildRow(task);
                _rows.Children.Add(row);
                _rowsByTask[task.TaskId] = (row, entry);
            }
            UpdateTitle();
        });
    }

    // ── content ───────────────────────────────────────────────────────
    private static UIElement BuildContent()
    {
        var root = new Border
        {
            CornerRadius = new CornerRadius(14),
            Background = new SolidColorBrush(
                Windows.UI.Color.FromArgb(245, 24, 24, 27)),
            BorderBrush = new SolidColorBrush(
                Windows.UI.Color.FromArgb(120, 245, 158, 11)),
            BorderThickness = new Thickness(1),
            Padding = new Thickness(10),
        };

        // Header: ☰ menu + title + collapse + dismiss. The title and empty
        // header space are the drag handle — the ☰ is a REAL menu button so
        // it must not look like a dead hamburger.
        var header = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 8,
            VerticalAlignment = VerticalAlignment.Center,
        };
        _title = new TextBlock
        {
            Text = "Toustovač",
            FontSize = 13,
            FontWeight = Microsoft.UI.Text.FontWeights.SemiBold,
            VerticalAlignment = VerticalAlignment.Center,
            MinWidth = 150,
        };
        var menuBtn = new Button
        {
            Content = "☰",
            Padding = new Thickness(8, 2, 8, 2),
            FontSize = 13,
            MinWidth = 28,
            MinHeight = 24,
        };
        ToolTipService.SetToolTip(menuBtn, "Menu");
        var menu = new MenuFlyout();
        var clearItem = new MenuFlyoutItem { Text = "Clear finished" };
        clearItem.Click += (_, _) => _onClearFinished?.Invoke();
        var collapseItem = new MenuFlyoutItem { Text = "Collapse" };
        collapseItem.Click += (_, _) => SetCollapsed(!_collapsed);
        var dismissItem = new MenuFlyoutItem { Text = "Dismiss" };
        dismissItem.Click += (_, _) => Hide();
        menu.Items.Add(clearItem);
        menu.Items.Add(collapseItem);
        menu.Items.Add(new MenuFlyoutSeparator());
        menu.Items.Add(dismissItem);
        menuBtn.Click += (_, _) =>
        {
            collapseItem.Text = _collapsed ? "Expand" : "Collapse";
            menu.ShowAt(menuBtn);
        };
        _collapseBtn = new Button
        {
            Content = "–",
            Padding = new Thickness(8, 2, 8, 2),
            FontSize = 12,
            MinWidth = 28,
            MinHeight = 24,
        };
        ToolTipService.SetToolTip(_collapseBtn, "Collapse / expand");
        _collapseBtn.Click += (_, _) => SetCollapsed(!_collapsed);
        // Dismiss: closes the overlay; a new task event re-shows it.
        var dismissBtn = new Button
        {
            Content = "✕",
            Padding = new Thickness(8, 2, 8, 2),
            FontSize = 12,
            MinWidth = 28,
            MinHeight = 24,
        };
        ToolTipService.SetToolTip(dismissBtn, "Dismiss");
        dismissBtn.Click += (_, _) => Hide();
        header.Children.Add(menuBtn);
        header.Children.Add(_title);
        header.Children.Add(_collapseBtn);
        header.Children.Add(dismissBtn);
        header.PointerPressed += OnHeaderPressed;
        header.PointerMoved += OnHeaderMoved;
        header.PointerReleased += OnHeaderReleased;
        header.PointerCaptureLost += (_, _) => _dragging = false;

        _rows = new StackPanel { Spacing = 4, MaxHeight = 320 };

        // Toast bar (transient completion notification).
        _toast = new Border
        {
            Background = new SolidColorBrush(
                Windows.UI.Color.FromArgb(230, 56, 189, 248)),
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(10, 6, 10, 6),
            Margin = new Thickness(0, 4, 0, 0),
            Visibility = Visibility.Collapsed,
        };
        _toastText = new TextBlock { FontSize = 12, TextWrapping = TextWrapping.Wrap };
        _toast.Child = _toastText;
        _toast.Tapped += (_, _) =>
        {
            _toast.Visibility = Visibility.Collapsed;
        };

        var stack = new StackPanel { Spacing = 6 };
        stack.Children.Add(header);
        stack.Children.Add(_toast);
        var scroller = new ScrollViewer
        {
            Content = _rows,
            MaxHeight = 320,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
            HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
        };
        stack.Children.Add(scroller);
        root.Child = stack;
        return root;
    }

    private static (Border row, TaskEntry task) BuildRow(TaskEntry task)
    {
        var glyph = new TextBlock
        {
            FontSize = 14,
            MinWidth = 20,
            VerticalAlignment = VerticalAlignment.Center,
        };
        var label = new TextBlock
        {
            FontSize = 12,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis,
        };
        var meta = new TextBlock
        {
            FontSize = 10,
            Opacity = 0.75,
            VerticalAlignment = VerticalAlignment.Center,
            TextTrimming = TextTrimming.CharacterEllipsis,
            MaxWidth = 200,
        };
        var row = new Border
        {
            CornerRadius = new CornerRadius(8),
            Padding = new Thickness(8, 4, 8, 4),
            Background = new SolidColorBrush(
                Windows.UI.Color.FromArgb(40, 255, 255, 255)),
        };
        var inner = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        var textCol = new StackPanel { Spacing = 1, VerticalAlignment = VerticalAlignment.Center };
        textCol.Children.Add(label);
        textCol.Children.Add(meta);
        inner.Children.Add(glyph);
        inner.Children.Add(textCol);
        row.Child = inner;

        var cancelBtn = new Button
        {
            Content = "✕",
            Padding = new Thickness(6, 0, 6, 0),
            FontSize = 11,
            MinWidth = 24,
            MinHeight = 20,
            VerticalAlignment = VerticalAlignment.Center,
            Visibility = Visibility.Collapsed,
        };
        cancelBtn.Click += (_, _) => _onCancelTask?.Invoke(task.TaskId);
        (inner as StackPanel)!.Children.Add(cancelBtn);

        var entry = task;
        row.Tapped += (_, _) =>
        {
            if (task.State == "completed" || task.State == "failed")
            {
                _onOpenTask?.Invoke(task.TaskId);
            }
        };
        row.Tag = (glyph, label, meta, cancelBtn);
        RenderRow(row, entry);
        return (row, entry);
    }

    private static void RenderRow(Border row, TaskEntry task)
    {
        if (row.Tag is not (TextBlock glyph, TextBlock label, TextBlock meta,
            Button cancelBtn))
        {
            return;
        }
        switch (task.State)
        {
            case "queued":
                glyph.Text = "⏳";
                glyph.Foreground = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(255, 200, 200, 200));
                label.Text = $"{task.ActionLabel} — queued #{task.Position + 1}";
                break;
            case "running":
                glyph.Text = "⟳";
                glyph.Foreground = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(255, 56, 189, 248));
                label.Text = $"{task.ActionLabel} — running";
                break;
            case "streaming":
                glyph.Text = "⟳";
                glyph.Foreground = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(255, 56, 189, 248));
                label.Text = $"{task.ActionLabel} — generating…";
                break;
            case "completed":
                glyph.Text = task.CacheHit ? "⚡" : "✓";
                glyph.Foreground = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(255, 74, 222, 128));
                label.Text = $"{task.ActionLabel} — done";
                break;
            case "failed":
                glyph.Text = "✗";
                glyph.Foreground = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(255, 248, 113, 113));
                label.Text = $"{task.ActionLabel} — {task.Failure}";
                break;
            default: // cancelled
                glyph.Text = "⏹";
                glyph.Foreground = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(255, 160, 160, 160));
                label.Text = $"{task.ActionLabel} — cancelled";
                break;
        }
        var elapsed = task.DurationMs is { } dms
            ? $" · {dms / 1000.0:F1}s"
            : "";
        var src = string.IsNullOrEmpty(task.ProcessName)
            ? task.SourceKind
            : task.ProcessName;
        var preview = task.State == "completed"
            ? $" · {EscapePreview(task.Preview)}"
            : "";
        meta.Text = $"{src}{elapsed}{preview}";
        cancelBtn.Visibility = task.IsActive
            ? Visibility.Visible : Visibility.Collapsed;
    }

    private static string EscapePreview(string preview)
    {
        var s = (preview ?? "").Replace("\r", " ").Replace("\n", " ").Trim();
        return s.Length > 40 ? s[..40] + "…" : s;
    }

    private static void UpdateTitle()
    {
        if (_title is null) return;
        var active = _rowsByTask.Values.Count(v => v.task.IsActive);
        var done = _rowsByTask.Values.Count(v => v.task.State == "completed");
        _title.Text = active > 0
            ? $"Toustovač · {active} running · {done} done"
            : done > 0
                ? $"Toustovač · {done} done"
                : "Toustovač";
    }

    // ── drag / collapse / toast ───────────────────────────────────────
    private static void OnHeaderPressed(object sender, PointerRoutedEventArgs e)
    {
        if (_window is null) return;
        // Buttons inside the header (⋯ menu, collapse, dismiss) must stay
        // clickable: never start a drag — and never capture the pointer —
        // from a button press, or the Button.Click is swallowed.
        if (IsInsideButton(e.OriginalSource)) return;
        _dragging = true;
        GetCursorPos(out var pt);
        _dragStart = new Point(pt.x, pt.y);
        _winStart = _window.AppWindow.Position;
        (sender as UIElement)?.CapturePointer(e.Pointer);
    }

    /// <summary>True when the pressed element is (or lives inside) a Button;
    /// used to keep header buttons clickable despite the drag handlers.</summary>
    private static bool IsInsideButton(object? source)
    {
        DependencyObject? node = source as DependencyObject;
        while (node is not null)
        {
            if (node is Button) return true;
            node = VisualTreeHelper.GetParent(node);
        }
        return false;
    }

    private static void OnHeaderMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging || _window is null) return;
        GetCursorPos(out var pt);
        var dx = pt.x - _dragStart.X;
        var dy = pt.y - _dragStart.Y;
        var pos = new Windows.Graphics.PointInt32(
            _winStart.X + (int)dx, _winStart.Y + (int)dy);
        _window.AppWindow.Move(pos);
    }

    private static void OnHeaderReleased(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging || _window is null) return;
        _dragging = false;
        (sender as UIElement)?.ReleasePointerCapture(e.Pointer);
        SavePosition(_window.AppWindow.Position);
    }

    private static void SetCollapsed(bool collapsed)
    {
        _collapsed = collapsed;
        if (_rows is not null)
        {
            _rows.Visibility = collapsed ? Visibility.Collapsed : Visibility.Visible;
        }
        if (_collapseBtn is not null)
        {
            _collapseBtn.Content = collapsed ? "+" : "–";
        }
        if (_window is not null)
        {
            var (_, _, sw, sh) = MonitorHelper.PrimaryScreenPhysical();
            if (collapsed)
            {
                _window.AppWindow.Resize(new Windows.Graphics.SizeInt32(260, 44));
            }
            else
            {
                _window.AppWindow.Resize(new Windows.Graphics.SizeInt32(360, 220));
            }
            var pos = _window.AppWindow.Position;
            if (pos.X + 360 > sw)
            {
                _window.AppWindow.Move(new Windows.Graphics.PointInt32(
                    (int)(sw - 360), pos.Y));
            }
            SavePosition(_window.AppWindow.Position);
        }
        EverywhereApp.Log($"queue overlay collapsed={collapsed}");
    }

    private static void Flash(Border row)
    {
        var story = new Microsoft.UI.Xaml.Media.Animation.Storyboard();
        var anim = new Microsoft.UI.Xaml.Media.Animation.ColorAnimation
        {
            From = Windows.UI.Color.FromArgb(220, 245, 158, 11),
            To = Windows.UI.Color.FromArgb(40, 255, 255, 255),
            Duration = new Duration(TimeSpan.FromMilliseconds(900)),
        };
        Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTarget(anim, row);
        Microsoft.UI.Xaml.Media.Animation.Storyboard.SetTargetProperty(
            anim, "(Border.Background).(SolidColorBrush.Color)");
        story.Children.Add(anim);
        story.Begin();
    }

    private static void EnsureToastTimer()
    {
        if (_toastTimer is not null) return;
        var dq = _window?.DispatcherQueue;
        if (dq is null) return;
        _toastTimer = dq.CreateTimer();
        _toastTimer.Interval = TimeSpan.FromSeconds(6);
        _toastTimer.IsRepeating = false;
        _toastTimer.Tick += (_, _) =>
        {
            if (_toast is not null)
            {
                _toast.Visibility = Visibility.Collapsed;
            }
        };
        _toastTimer.Start();
    }

    // ── position persistence ──────────────────────────────────────────
    private static (int x, int y)? LoadPosition()
    {
        try
        {
            if (!System.IO.File.Exists(UiStatePath)) return null;
            var doc = JsonDocument.Parse(
                System.IO.File.ReadAllText(UiStatePath));
            var root = doc.RootElement;
            if (root.TryGetProperty("collapsed", out var c))
            {
                _collapsed = c.GetBoolean();
            }
            if (root.TryGetProperty("x", out var xp)
                && root.TryGetProperty("y", out var yp))
            {
                return (xp.GetInt32(), yp.GetInt32());
            }
        }
        catch (Exception)
        {
        }
        return null;
    }

    private static void SavePosition(Windows.Graphics.PointInt32 pos)
    {
        try
        {
            var state = new Dictionary<string, object?>
            {
                ["x"] = pos.X,
                ["y"] = pos.Y,
                ["collapsed"] = _collapsed,
            };
            var dir = System.IO.Path.GetDirectoryName(UiStatePath);
            if (!string.IsNullOrEmpty(dir))
            {
                System.IO.Directory.CreateDirectory(dir);
            }
            System.IO.File.WriteAllText(
                UiStatePath,
                JsonSerializer.Serialize(state));
        }
        catch (Exception)
        {
        }
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetCursorPos(out POINT pt);

    [System.Runtime.InteropServices.StructLayout(
        System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct POINT { public int x; public int y; }
}