// The lightweight overlay: a rounded, animated "chocolate-bar" toolbar of
// icon buttons, plus result panel and shortcut legend. Fluent icons, an
// entrance animation, a hover highlight, arrow-key navigation (first button
// highlighted, Esc dismisses). Keyboard-navigable from first render; the
// toolbar never steals the origin focus identity (§Focus).
using System.Collections.Generic;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Media.Animation;
using Windows.System;

namespace Toastovac.Everywhere.Host.App;

public static class ToolbarWindow
{
    private static StackPanel? _root;
    private static Border? _card;
    private static readonly List<(Button btn, string id)> _buttons = new();
    private static int _focusIndex;
    private static Action<string>? _onAction;
    private static Action? _onDismiss;

    // Fluent-system glyph icons (Segoe Fluent Icons) per action.
    private static readonly (string id, string glyph, string label, string key)[]
        _defs =
    {
        // Segoe Fluent Icons glyphs, matched to each action's meaning.
        ("rewrite",      "\uE70F", "Rewrite",      "R"),   // Edit / pencil
        ("proofread",    "\uE73E", "Proofread",    "P"),   // CheckMark
        ("alternatives", "\uE8FD", "Alternatives", "A"),   // BulletedList
        ("explain",      "\uE946", "Explain",      "E"),   // Info
        ("translate",    "\uE774", "Translate",    "T"),   // Globe
        ("prompt",       "\uE943", "Prompt",       "U"),   // ChatBubbles
        ("subtitles",    "\uED1E", "Subtitles",    "S"),   // Caption/CC
        ("coach",        "\uE95B", "Coach",        "C"),   // Headset
        ("ocr",          "\uE7B8", "Read Screen",  "O"),   // Scan / crop
    };

    public static UIElement Build(Window window,
        Action<string> onAction, Action onDismiss)
    {
        _onAction = onAction;
        _onDismiss = onDismiss;
        _focusIndex = 0;
        _buttons.Clear();

        _root = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            Spacing = 6,
            Padding = new Thickness(10),
            HorizontalAlignment = HorizontalAlignment.Center,
        };
        foreach (var (id, glyph, label, key) in _defs)
        {
            AddButton(id, glyph, label, key);
        }

        // Rounded card container so the bar has no harsh square edges.
        _card = new Border
        {
            CornerRadius = new CornerRadius(14),
            Padding = new Thickness(4),
            Background = new SolidColorBrush(
                Windows.UI.Color.FromArgb(240, 24, 24, 27)),
            BorderBrush = new SolidColorBrush(
                Windows.UI.Color.FromArgb(90, 245, 158, 11)),
            BorderThickness = new Thickness(1),
            Child = _root,
        };
        window.Content = _card;

        window.Content.KeyDown += OnToolbarKeyDown;
        window.Content.PointerEntered += (_, _) =>
        {
            if (_card is not null)
            {
                _card.BorderBrush = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(160, 245, 158, 11));
            }
        };
        window.Content.PointerExited += (_, _) =>
        {
            if (_card is not null)
            {
                _card.BorderBrush = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(90, 245, 158, 11));
            }
        };
        return _card;
    }

    private static void AddButton(string id, string glyph, string label,
        string key)
    {
        if (_root is null) return;
        var button = new Button
        {
            Tag = id,
            Padding = new Thickness(12, 10, 12, 10),
            MinHeight = 56,
            MinWidth = 72,
            CornerRadius = new CornerRadius(10),
            Background = new SolidColorBrush(
                Windows.UI.Color.FromArgb(0, 0, 0, 0)),
        };
        var stack = new StackPanel { Spacing = 2 };
        stack.Children.Add(new FontIcon
        {
            Glyph = glyph,
            FontSize = 24,
            FontFamily = new FontFamily("Segoe Fluent Icons, Segoe MDL2 Assets"),
            HorizontalAlignment = HorizontalAlignment.Center,
        });
        stack.Children.Add(new TextBlock
        {
            Text = $"{label} ({key})",
            FontSize = 11,
            HorizontalAlignment = HorizontalAlignment.Center,
            Opacity = 0.85,
        });
        button.Content = stack;
        button.Click += (_, _) => _onAction?.Invoke(id);
        _root.Children.Add(button);
        _buttons.Add((button, id));
    }

    // ── keyboard navigation (MS-DOS TUI style: arrows + Enter + Esc) ──
    private static void OnToolbarKeyDown(object sender, KeyRoutedEventArgs e)
    {
        if (_buttons.Count == 0) return;
        switch (e.Key)
        {
            case VirtualKey.Right:
                MoveFocus(1);
                e.Handled = true;
                break;
            case VirtualKey.Left:
                MoveFocus(-1);
                e.Handled = true;
                break;
            case VirtualKey.Enter:
            case VirtualKey.Space:
                _onAction?.Invoke(_buttons[_focusIndex].id);
                e.Handled = true;
                break;
            case VirtualKey.Escape:
                _onDismiss?.Invoke();
                e.Handled = true;
                break;
        }
    }

    private static void MoveFocus(int delta)
    {
        _focusIndex = (_focusIndex + delta + _buttons.Count) % _buttons.Count;
        HighlightFocus();
    }

    private static void HighlightFocus()
    {
        for (var i = 0; i < _buttons.Count; i++)
        {
            var b = _buttons[i].btn;
            var on = i == _focusIndex;
            b.Background = new SolidColorBrush(on
                ? Windows.UI.Color.FromArgb(200, 245, 158, 11)   // amber highlight
                : Windows.UI.Color.FromArgb(0, 0, 0, 0));
            if (on)
            {
                b.Focus(FocusState.Programmatic);
            }
        }
    }

    /// <summary>Drive the toolbar from the global hook (the window is
    /// non-activating, so the keyboard never reaches it directly). Handles
    /// arrows/Enter/Esc; returns true when the key was consumed.</summary>
    public static bool HandleNavKey(uint vk)
    {
        // The result panel is up: only Esc closes it; arrows/Enter must NOT
        // fire a stale toolbar action (the toolbar buttons are gone).
        if (ResultPanelActive)
        {
            if (vk == 0x1B)  // Esc
            {
                ResultPanelActive = false;
                _onDismiss?.Invoke();
            }
            return true;
        }
        if (_buttons.Count == 0) return false;
        switch (vk)
        {
            case 0x27: case 0x28:  // Right / Down
                MoveFocus(1);
                return true;
            case 0x25: case 0x26:  // Left / Up
                MoveFocus(-1);
                return true;
            case 0x0D: case 0x20:  // Enter / Space
                _onAction?.Invoke(_buttons[_focusIndex].id);
                return true;
            case 0x1B:  // Esc
                _onDismiss?.Invoke();
                return true;
        }
        return false;
    }

    /// <summary>Show the toolbar with a pop-in entrance animation and the
    /// first button highlighted.</summary>
    private static void PlayEntrance(Border card)
    {
        card.Opacity = 0;
        var scale = new ScaleTransform
        {
            ScaleX = 0.85, ScaleY = 0.85,
            CenterX = 0, CenterY = 0,
        };
        card.RenderTransform = scale;
        var story = new Storyboard();
        var fade = new DoubleAnimation
        {
            To = 1,
            Duration = new Duration(TimeSpan.FromMilliseconds(180)),
            EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut },
        };
        Storyboard.SetTarget(fade, card);
        Storyboard.SetTargetProperty(fade, "Opacity");
        story.Children.Add(fade);
        foreach (var (prop, to) in new[] { ("ScaleX", 1.0), ("ScaleY", 1.0) })
        {
            var anim = new DoubleAnimation
            {
                To = to,
                Duration = new Duration(TimeSpan.FromMilliseconds(220)),
                EasingFunction = new CubicEase { EasingMode = EasingMode.EaseOut },
            };
            Storyboard.SetTarget(anim, scale);
            Storyboard.SetTargetProperty(anim, prop);
            story.Children.Add(anim);
        }
        story.Begin();
    }

    public static void ShowAt(Window? window, (double x, double y)? anchor)
    {
        if (window is null || _root is null)
        {
            return;
        }
        var hwnd = WinRT.Interop.WindowNative.GetWindowHandle(window);
        var scale = Windowing.MonitorHelper.DpiScaleForWindow(hwnd, anchor, 1.0);
        _root.Measure(new Windows.Foundation.Size(double.PositiveInfinity,
            double.PositiveInfinity));
        var desired = _root.DesiredSize;
        var cardPad = 12 * scale;
        var width = (desired.Width * scale) + cardPad * 2;
        var height = (desired.Height * scale) + cardPad * 2;

        var (sx, sy, sw, sh) = Windowing.MonitorHelper.PrimaryScreenPhysical();
        var ax = anchor?.x ?? (sx + (sw - width) / 2);
        var ay = anchor?.y ?? (sy + sh - height - 40);
        var x = Math.Max(sx, Math.Min(ax, sx + sw - width));
        var y = Math.Max(sy, Math.Min(ay, sy + sh - height));
        window.AppWindow.Resize(new Windows.Graphics.SizeInt32(
            (int)Math.Ceiling(width), (int)Math.Ceiling(height)));
        window.AppWindow.Move(new Windows.Graphics.PointInt32((int)x, (int)y));
        window.Activate();

        if (_card is not null)
        {
            PlayEntrance(_card);
        }
        _focusIndex = 0;
        HighlightFocus();
    }

    /// <summary>True while the result panel is up: the toolbar's TUI nav
    /// (arrows/Enter) is disabled so a stray keypress can't fire a stale
    /// action. Esc still closes the panel.</summary>
    public static bool ResultPanelActive { get; set; }

    public static void ShowResult(Window? window, string result,
        string capability, Action onInsert, Action onCopy,
        string action = "", Action<string>? onRetranslate = null)
    {
        if (window?.Content is not Border card
            || card.Child is not StackPanel panel)
        {
            return;
        }
        ResultPanelActive = true;
        panel.Children.Clear();

        var content = new StackPanel { Spacing = 10, Padding = new Thickness(8) };

        // Translate: a target-language picker so the user chooses the language
        // (and can re-translate). Others get just the result.
        if (action == "translate" && onRetranslate is not null)
        {
            var langRow = new StackPanel
            { Orientation = Orientation.Horizontal, Spacing = 8 };
            langRow.Children.Add(new TextBlock { Text = "→", FontSize = 16,
                VerticalAlignment = VerticalAlignment.Center });
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
            var goBtn = new Button { Content = "🌐 Translate", FontSize = 14,
                Padding = new Thickness(12, 6, 12, 6), CornerRadius = new CornerRadius(8) };
            goBtn.Click += (_, _) =>
            {
                var code = (langCombo.SelectedItem as ComboBoxItem)?.Tag as string;
                if (!string.IsNullOrEmpty(code))
                {
                    onRetranslate(code);
                }
            };
            langRow.Children.Add(langCombo);
            langRow.Children.Add(goBtn);
            content.Children.Add(langRow);
        }

        // The result text, scrollable and selectable.
        var scroller = new ScrollViewer { MaxHeight = 300 };
        scroller.Content = new TextBlock
        {
            Text = result,
            TextWrapping = TextWrapping.Wrap,
            IsTextSelectionEnabled = true,
            FontSize = 15,
        };
        content.Children.Add(scroller);

        // Action row: Insert (editable targets only), Copy, Close.
        var row = new StackPanel { Orientation = Orientation.Horizontal,
            Spacing = 8 };
        if (capability != Protocol.ReplaceCapability.CopyOnly)
        {
            var insert = new Button
            {
                Content = "⤵ Insert",
                Padding = new Thickness(14, 8, 14, 8),
                CornerRadius = new CornerRadius(8),
            };
            insert.Click += (_, _) => { ResultPanelActive = false; onInsert(); };
            row.Children.Add(insert);
        }
        var copy = new Button
        {
            Content = "📋 Copy",
            Padding = new Thickness(14, 8, 14, 8),
            CornerRadius = new CornerRadius(8),
        };
        copy.Click += (_, _) => onCopy();
        row.Children.Add(copy);
        var close = new Button
        {
            Content = "✕ Close",
            Padding = new Thickness(14, 8, 14, 8),
            CornerRadius = new CornerRadius(8),
        };
        close.Click += (_, _) => { ResultPanelActive = false; Hide(window); };
        row.Children.Add(close);
        content.Children.Add(row);
        panel.Children.Add(content);
    }

    public static void ShowHelp(Window? window)
    {
        if (window?.Content is not Border card
            || card.Child is not StackPanel panel)
        {
            return;
        }
        panel.Children.Clear();
        panel.Children.Add(new TextBlock
        {
            Text = "Ctrl+Shift+Space toolbar · arrows navigate · Enter run · "
                 + "R/P/A/E/T/U/S/C/O actions · Ctrl+Shift+letter direct · "
                 + "Alt+drag OCR · Ctrl+/ help · Esc dismiss",
            TextWrapping = TextWrapping.Wrap,
        });
    }

    public static void Hide(Window? window)
    {
        if (window is null)
        {
            return;
        }
        try
        {
            window.AppWindow.Hide();
        }
        catch (Exception)
        {
        }
    }
}
