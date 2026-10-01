// Persistent result window: full result text, Copy that stays on the
// clipboard, Close that does not tear down the queue overlay, and a Retry
// button for failed tasks. Results render asynchronously (a truncated result
// is pulled from the broker on demand with a loading state) and the window is
// explicitly brought to the front so it cannot hide behind the fullscreen OCR
// selector (§Overlay conflict). Insert stays available for replace-capable
// snapshots.
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Media;
using Toastovac.Everywhere.Host.Protocol;
using Toastovac.Everywhere.Host.Replacement;
using Windows.ApplicationModel.DataTransfer;

namespace Toastovac.Everywhere.Host.App;

public static class ResultOverlay
{
    private static Window? _window;
    private static TextBlock? _body;
    private static TextBlock? _status;
    private static string _payload = "";
    private static string _action = "";
    private static string _capability = ReplaceCapability.CopyOnly;
    private static string _failure = "";
    private static string _taskId = "";
    private static Action<string>? _onRetranslate;
    private static Action? _onRetry;

    public static bool IsVisible => _window is not null;

    /// <summary>Show (or refresh) one task's result. Runs on the UI thread;
    /// a truncated result is fetched from the broker asynchronously with a
    /// loading state. ``onRetry`` re-runs the failed task's action.</summary>
    public static void ShowTask(TaskEntry task, Action<string> onRetranslate,
        Action? onRetry = null)
    {
        _taskId = task.TaskId;
        _action = task.Action;
        _capability = task.Capability;
        _failure = task.Failure;
        _onRetranslate = onRetranslate;
        if (task.State == "completed")
        {
            if (task.Result.Length > 0 && !task.Truncated)
            {
                Show(task.Result, task.Capability, task.Action, task.CacheHit,
                    failure: "", onRetry: null);
                return;
            }
            // Truncated or empty: fetch the full result from the broker.
            Show("", task.Capability, task.Action, task.CacheHit,
                failure: "", onRetry: null, loading: "Loading result…");
            _ = FetchResultAsync(task.TaskId);
            return;
        }
        if (task.State == "failed")
        {
            Show("", task.Capability, task.Action, false,
                failure: task.Failure, onRetry: onRetry);
            return;
        }
        Show("", task.Capability, task.Action, false,
            failure: "", onRetry: null, loading: "Task pending…");
    }

    private static async System.Threading.Tasks.Task FetchResultAsync(
        string taskId)
    {
        try
        {
            var pipe = CurrentPipe;
            if (pipe is null) return;
            var request = new Dictionary<string, object?>
            {
                ["protocol"] = Framing.ProtocolId,
                ["kind"] = "task_result",
                ["request_id"] = Guid.NewGuid().ToString("N")[..12],
                ["task_id"] = taskId,
            };
            var reply = await pipe.RoundTripAsync(
                System.Text.Json.JsonSerializer.SerializeToElement(request),
                8000);
            _window?.DispatcherQueue.TryEnqueue(() =>
            {
                if (reply is not null
                    && reply.Value.TryGetProperty("result", out var r)
                    && r.ValueKind == System.Text.Json.JsonValueKind.String)
                {
                    Show(r.GetString() ?? "", _capability, _action, false,
                        failure: "", onRetry: null);
                }
                else
                {
                    Show("", _capability, _action, false,
                        failure: FailureCodes.PipeDisconnected,
                        onRetry: null);
                }
            });
        }
        catch (Exception ex)
        {
            EverywhereApp.Log($"result fetch failed: {ex.GetType().Name}");
            _window?.DispatcherQueue.TryEnqueue(() =>
                Show("", _capability, _action, false,
                    failure: FailureCodes.PipeDisconnected, onRetry: null));
        }
    }

    /// <summary>The broker pipe shared with the app (set by EverywhereApp).</summary>
    internal static Pipe.EverywherePipeClient? CurrentPipe { get; set; }

    public static void Show(string result, string capability, string action,
        bool cacheHit, string failure, Action? onRetry,
        string loading = "", Action<string>? onRetranslate = null)
    {
        _payload = result ?? "";
        _capability = capability;
        _action = action;
        _failure = failure;
        _onRetry = onRetry;
        if (onRetranslate is not null) _onRetranslate = onRetranslate;
        if (_window is null)
        {
            var win = new Window { Title = "Toustovač Result" };
            _window = win;
            win.Closed += (_, _) =>
            {
                _window = null;
                _body = null;
                _status = null;
                ToolbarWindow.ResultPanelActive = false;
            };
            Windowing.Chrome.MakeToolOverlay(win);
        }
        _window.Content = Build(cacheHit, loading);
        SizeAndShow();
        // The OCR selector is a fullscreen topmost window; a finished task's
        // panel must never hide behind it (§Overlay conflict).
        OcrOverlay.CloseIfNotDragging();
        EverywhereApp.Log($"result.show task={_taskId} action={_action} "
            + $"chars={_payload.Length} failure={(failure == "" ? "-" : failure)}");
    }

    public static void Hide()
    {
        var win = _window;
        _window = null;
        _body = null;
        _status = null;
        ToolbarWindow.ResultPanelActive = false;
        if (win is null) return;
        try { win.AppWindow.Hide(); } catch (Exception) { }
        try { win.Close(); } catch (Exception) { }
        EverywhereApp.Log("result.hide");
    }

    private static UIElement Build(bool cacheHit, string loading)
    {
        var root = new Border
        {
            CornerRadius = new CornerRadius(14),
            Padding = new Thickness(14),
            Background = new SolidColorBrush(Windows.UI.Color.FromArgb(250, 24, 24, 27)),
            BorderBrush = new SolidColorBrush(Windows.UI.Color.FromArgb(160, 245, 158, 11)),
            BorderThickness = new Thickness(1),
        };
        var content = new StackPanel { Spacing = 10 };

        var titleRow = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 8,
        };
        titleRow.Children.Add(new TextBlock
        {
            Text = string.IsNullOrEmpty(_action) ? "Result" : _action,
            FontSize = 16,
            Opacity = 0.85,
            VerticalAlignment = VerticalAlignment.Center,
        });
        if (cacheHit)
        {
            titleRow.Children.Add(new TextBlock
            {
                Text = "⚡ cached",
                FontSize = 11,
                Opacity = 0.7,
                VerticalAlignment = VerticalAlignment.Center,
            });
        }
        content.Children.Add(titleRow);

        if (_action == "translate" && _onRetranslate is not null)
        {
            var langRow = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
            var langCombo = new ComboBox { MinWidth = 160, FontSize = 14 };
            var langs = new (string label, string code)[]
            {
                ("Čeština", "cs"), ("English", "en"), ("Deutsch", "de"),
                ("Español", "es"), ("Français", "fr"), ("Polski", "pl"),
                ("Nederlands", "nl"), ("Slovenčina", "sk"),
                ("Tiếng Việt", "vi"), ("한국어", "ko"), ("中文", "zh"),
                ("العربية", "ar"),
            };
            foreach (var (label, code) in langs)
            {
                langCombo.Items.Add(new ComboBoxItem { Content = label, Tag = code });
            }
            langCombo.SelectedIndex = 0;
            var goBtn = new Button
            {
                Content = "🌐 Retranslate",
                Padding = new Thickness(12, 6, 12, 6),
                CornerRadius = new CornerRadius(8),
            };
            goBtn.Click += (_, _) =>
            {
                var code = (langCombo.SelectedItem as ComboBoxItem)?.Tag as string;
                if (!string.IsNullOrEmpty(code)) _onRetranslate?.Invoke(code);
            };
            langRow.Children.Add(langCombo);
            langRow.Children.Add(goBtn);
            content.Children.Add(langRow);
        }

        if (!string.IsNullOrEmpty(loading))
        {
            _status = new TextBlock
            {
                Text = loading,
                FontSize = 14,
                Opacity = 0.85,
            };
            content.Children.Add(_status);
        }
        else if (!string.IsNullOrEmpty(_failure))
        {
            _status = new TextBlock
            {
                Text = $"✗ {_failure}",
                FontSize = 14,
                Foreground = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(255, 248, 113, 113)),
            };
            content.Children.Add(_status);
        }

        // The payload is display-only: a WinUI TextBox crashes this build with
        // FileNotFoundException (0x80070002) during render in the single-file
        // layout, so the result is shown as a read-only TextBlock instead.
        // Copy / Insert operate on the same payload.
        _body = new TextBlock
        {
            Text = _payload,
            TextWrapping = TextWrapping.Wrap,
            FontSize = 15,
            MinHeight = 220,
            MaxHeight = 480,
        };
        content.Children.Add(_body);

        var row = new StackPanel { Orientation = Orientation.Horizontal, Spacing = 8 };
        if (_capability != ReplaceCapability.CopyOnly && _action != "error"
            && string.IsNullOrEmpty(_failure))
        {
            var insert = new Button
            {
                Content = "⤵ Insert",
                Padding = new Thickness(14, 8, 14, 8),
                CornerRadius = new CornerRadius(8),
            };
            insert.Click += (_, _) =>
            {
                EverywhereApp.Instance?.Insert();
            };
            row.Children.Add(insert);
        }
        if (!string.IsNullOrEmpty(_failure) && _onRetry is not null)
        {
            var retry = new Button
            {
                Content = "↻ Retry",
                Padding = new Thickness(14, 8, 14, 8),
                CornerRadius = new CornerRadius(8),
            };
            retry.Click += (_, _) => _onRetry?.Invoke();
            row.Children.Add(retry);
        }
        var copy = new Button
        {
            Content = "📋 Copy",
            Padding = new Thickness(14, 8, 14, 8),
            CornerRadius = new CornerRadius(8),
        };
        copy.Click += (_, _) =>
        {
            var text = _payload;
            try
            {
                var package = new DataPackage();
                package.RequestedOperation = DataPackageOperation.Copy;
                package.SetText(text ?? "");
                Clipboard.SetContent(package);
                Clipboard.Flush();
            }
            catch (Exception)
            {
                Replacer.Apply(ReplaceCapability.CopyOnly, text ?? "", null, false);
            }
        };
        row.Children.Add(copy);
        var close = new Button
        {
            Content = "✕ Close",
            Padding = new Thickness(14, 8, 14, 8),
            CornerRadius = new CornerRadius(8),
        };
        close.Click += (_, _) => Hide();
        row.Children.Add(close);
        content.Children.Add(row);
        root.Child = content;
        return root;
    }

    private static void SizeAndShow()
    {
        if (_window is null) return;
        var (sx, sy, sw, sh) = Windowing.MonitorHelper.PrimaryScreenPhysical();
        var width = Math.Min(720, Math.Max(420, sw * 0.38));
        var height = Math.Min(640, Math.Max(360, sh * 0.45));
        _window.AppWindow.Resize(new Windows.Graphics.SizeInt32((int)width, (int)height));
        _window.AppWindow.Move(new Windows.Graphics.PointInt32(
            (int)(sx + (sw - width) / 2),
            (int)(sy + (sh - height) / 2)));
        try { _window.AppWindow.Show(); } catch (Exception) { }
        try { _window.Activate(); } catch (Exception) { }
        // Bring-to-front fix: the OCR selector is a fullscreen topmost window;
        // a plain Show can land the result panel underneath it. Force the
        // topmost band and foreground explicitly.
        try
        {
            var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(_window);
            Windowing.Chrome.BringToFront(hwnd);
        }
        catch (Exception)
        {
        }
    }
}
