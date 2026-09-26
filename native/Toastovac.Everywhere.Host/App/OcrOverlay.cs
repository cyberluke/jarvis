// Screen Reading (OCR) overlay. Hold Alt and drag a rectangle over
// non-selectable text; the region is captured, OCR'd locally (OneOCR via the
// broker), and the recognized words are drawn as clickable boxes over the
// region. Click a word to copy it. "Translate page" overlays the translated
// text. Esc cancels. The drag gesture is the only thing intercepted — Alt is
// not globally suppressed (§16).
using System.Text.Json;
using Microsoft.UI.Xaml;
using Microsoft.UI.Xaml.Controls;
using Microsoft.UI.Xaml.Input;
using Microsoft.UI.Xaml.Media;
using Microsoft.UI.Xaml.Shapes;
using Toastovac.Everywhere.Host.Capture;
using Toastovac.Everywhere.Host.Pipe;

namespace Toastovac.Everywhere.Host.App;

public static class OcrOverlay
{
    private static Window? _window;
    private static Canvas? _canvas;
    private static Canvas? _wordsCanvas;
    private static TextBlock? _status;
    private static EverywherePipeClient? _pipe;
    private static Microsoft.UI.Dispatching.DispatcherQueue? _dq;

    // Drag state.
    private static bool _dragging;
    private static Windows.Foundation.Point _dragStart;
    private static Microsoft.UI.Xaml.Shapes.Rectangle? _selectionRect;
    private static PixelRect _capturedRegion;

    public static void Toggle(Window? anchor, EverywherePipeClient pipe)
    {
        _pipe = pipe;
        if (_window is not null)
        {
            Close();
            return;
        }
        var dq = anchor?.DispatcherQueue
            ?? Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();
        if (dq is null) return;
        dq.TryEnqueue(CreateSelector);
    }

    // ── selector window (fullscreen, transparent, drag a rectangle) ───
    private static void CreateSelector()
    {
        try
        {
            _dq = Microsoft.UI.Dispatching.DispatcherQueue.GetForCurrentThread();
            var win = new Window { Title = "Toustovač OCR" };
            _window = win;
            // Fully transparent content background; only the selection
            // rectangle, the status pill and the word boxes are drawn. On an
            // HDR display the window must be transparent so the desktop shows
            // through during the drag.
            win.SystemBackdrop = null;
            _canvas = new Canvas
            {
                Background = new Microsoft.UI.Xaml.Media.SolidColorBrush(
                    Microsoft.UI.Colors.Transparent),
            };
            _status = new TextBlock
            {
                Text = "Drag a rectangle over the text · Esc to cancel",
                FontSize = 22,
                Foreground = new SolidColorBrush(Microsoft.UI.Colors.White),
            };
            // Dark pill behind the status text so it reads on light content.
            var statusPill = new Border
            {
                Background = new SolidColorBrush(
                    Windows.UI.Color.FromArgb(160, 24, 24, 27)),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(16, 8, 16, 8),
                Child = _status,
            };
            Canvas.SetLeft(statusPill, 24);
            Canvas.SetTop(statusPill, 24);
            _canvas.Children.Add(statusPill);
            win.Content = _canvas;
            Windowing.Chrome.MakeToolOverlay(win);
            // Whole-window alpha: on HDR the DirectComposition content ignores
            // color keys, so make the entire window see-through instead. 60/255
            // keeps the selection rectangle + status pill readable while the
            // desktop shows through clearly.
            Windowing.Chrome.SetWholeWindowAlpha(win, 60);
            // Fullscreen over the primary screen.
            var (sx, sy, sw, sh) = Windowing.MonitorHelper.PrimaryScreenPhysical();
            win.AppWindow.Resize(new Windows.Graphics.SizeInt32((int)sw, (int)sh));
            win.AppWindow.Move(new Windows.Graphics.PointInt32((int)sx, (int)sy));

            _canvas.PointerPressed += OnPointerPressed;
            _canvas.PointerMoved += OnPointerMoved;
            _canvas.PointerReleased += OnPointerReleased;
            // Esc cancels. The window is non-activating so it never gets
            // keyboard focus; intercept Esc in the global low-level hook for
            // the duration of the selection gesture (§16).
            if (EverywhereApp.Hotkeys is not null)
            {
                EverywhereApp.Hotkeys.SessionEscapeHandler = () =>
                {
                    _dq?.TryEnqueue(Close);
                    return true;
                };
            }
            win.Activate();
            win.Closed += (_, _) =>
            {
                if (EverywhereApp.Hotkeys is not null)
                {
                    EverywhereApp.Hotkeys.SessionEscapeHandler = null;
                }
            };
            EverywhereApp.Log("ocr selector shown");
        }
        catch (Exception ex)
        {
            EverywhereApp.Log($"ocr selector failed: {ex.GetType().Name}: "
                + ex.Message);
            _window = null;
        }
    }

    private static void OnPointerPressed(object sender, PointerRoutedEventArgs e)
    {
        if (_canvas is null) return;
        _dragging = true;
        _dragStart = e.GetCurrentPoint(_canvas).Position;
        if (_selectionRect is not null)
        {
            _canvas.Children.Remove(_selectionRect);
        }
        _selectionRect = new Microsoft.UI.Xaml.Shapes.Rectangle
        {
            Stroke = new SolidColorBrush(Microsoft.UI.Colors.Orange),
            StrokeThickness = 3,
            Fill = new SolidColorBrush(
                Windows.UI.Color.FromArgb(40, 245, 158, 11)),
        };
        _canvas.Children.Add(_selectionRect);
        _canvas.CapturePointer(e.Pointer);
    }

    private static void OnPointerMoved(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging || _canvas is null || _selectionRect is null) return;
        var pos = e.GetCurrentPoint(_canvas).Position;
        var x = Math.Min(_dragStart.X, pos.X);
        var y = Math.Min(_dragStart.Y, pos.Y);
        var w = Math.Abs(pos.X - _dragStart.X);
        var h = Math.Abs(pos.Y - _dragStart.Y);
        Canvas.SetLeft(_selectionRect, x);
        Canvas.SetTop(_selectionRect, y);
        _selectionRect.Width = w;
        _selectionRect.Height = h;
    }

    private static void OnPointerReleased(object sender, PointerRoutedEventArgs e)
    {
        if (!_dragging || _canvas is null || _selectionRect is null) return;
        _dragging = false;
        _canvas.ReleasePointerCapture(e.Pointer);
        var pos = e.GetCurrentPoint(_canvas).Position;
        var x = (int)Math.Min(_dragStart.X, pos.X);
        var y = (int)Math.Min(_dragStart.Y, pos.Y);
        var w = (int)Math.Abs(pos.X - _dragStart.X);
        var h = (int)Math.Abs(pos.Y - _dragStart.Y);
        if (w < 8 || h < 8)
        {
            _canvas.Children.Remove(_selectionRect);
            return;  // too small to OCR
        }
        _capturedRegion = new PixelRect(x, y, w, h);
        _ = RecognizeRegionAsync();
    }

    // ── capture + OCR + result ────────────────────────────────────────
    private static async System.Threading.Tasks.Task RecognizeRegionAsync()
    {
        if (_status is not null) _status.Text = "Reading…";
        if (_canvas is not null && _selectionRect is not null)
        {
            _canvas.Children.Remove(_selectionRect);
            _selectionRect = null;
        }
        // Capture the region to a temp PNG.
        var region = _capturedRegion;
        var png = System.IO.Path.Combine(System.IO.Path.GetTempPath(),
            $"toustovac-ocr-{Guid.NewGuid():N}.png");
        try
        {
            var bitmap = Capture.RegionCapturer.CaptureRegion(region);
            if (bitmap is null)
            {
                if (_status is not null) _status.Text = "Capture failed.";
                return;
            }
            using (var stream = new Windows.Storage.Streams
                .InMemoryRandomAccessStream())
            {
                var encoder = await Windows.Graphics.Imaging.BitmapEncoder
                    .CreateAsync(Windows.Graphics.Imaging.BitmapEncoder
                        .PngEncoderId, stream);
                encoder.SetSoftwareBitmap(bitmap);
                await encoder.FlushAsync();
                stream.Seek(0);
                var reader = new Windows.Storage.Streams.DataReader(stream);
                await reader.LoadAsync((uint)stream.Size);
                var bytes = new byte[reader.UnconsumedBufferLength];
                reader.ReadBytes(bytes);
                reader.DetachStream();
                await System.IO.File.WriteAllBytesAsync(png, bytes);
            }
            bitmap.Dispose();

            // Ask the broker to OCR it.
            var reply = _pipe?.RoundTrip(BuildCmd("recognize",
                ("image_path", png), ("region_x", region.X),
                ("region_y", region.Y)));
            if (reply is null || !reply.Value.TryGetProperty("lines", out var lines))
            {
                if (_status is not null) _status.Text = "No text found.";
                return;
            }
            RenderWords(reply.Value, region);
        }
        catch (Exception ex)
        {
            EverywhereApp.Log($"ocr recognize failed: {ex.GetType().Name}");
            if (_status is not null) _status.Text = "OCR failed.";
        }
        finally
        {
            try { if (System.IO.File.Exists(png)) System.IO.File.Delete(png); }
            catch (Exception) { }
        }
    }

    // ── clickable word boxes ──────────────────────────────────────────
    private static void RenderWords(JsonElement result, PixelRect region)
    {
        _dq?.TryEnqueue(() =>
        {
            if (_canvas is null) return;
            _canvas.Children.Clear();
            _canvas.Background = new SolidColorBrush(
                Windows.UI.Color.FromArgb(1, 0, 0, 0));  // near-transparent
            _wordsCanvas = new Canvas();
            _canvas.Children.Add(_wordsCanvas);

            var allText = result.TryGetProperty("text", out var tt)
                ? tt.GetString() ?? "" : "";

            if (result.TryGetProperty("lines", out var lines))
            {
                foreach (var line in lines.EnumerateArray())
                {
                    if (!line.TryGetProperty("words", out var words)) continue;
                    foreach (var word in words.EnumerateArray())
                    {
                        var text = word.GetProperty("text").GetString() ?? "";
                        if (!word.TryGetProperty("quad", out var q)
                            || q.GetArrayLength() < 8) continue;
                        var qa = q.EnumerateArray()
                            .Select(v => v.GetDouble()).ToArray();
                        var bx = Math.Min(Math.Min(qa[0], qa[2]),
                            Math.Min(qa[4], qa[6])) + region.X;
                        var by = Math.Min(Math.Min(qa[1], qa[3]),
                            Math.Min(qa[5], qa[7])) + region.Y;
                        var bw = Math.Max(Math.Max(qa[0], qa[2]),
                            Math.Max(qa[4], qa[6])) + region.X - bx;
                        var bh = Math.Max(Math.Max(qa[1], qa[3]),
                            Math.Max(qa[5], qa[7])) + region.Y - by;
                        AddWordBox(text, bx, by, bw, bh);
                    }
                }
            }

            // Bottom action row.
            var row = new StackPanel { Orientation = Orientation.Horizontal,
                Spacing = 12 };
            var copyAll = new Button { Content = "📋 Copy all", FontSize = 16,
                MinHeight = 44 };
            copyAll.Click += (_, _) => CopyText(allText);
            var translate = new Button { Content = "🌐 Translate page",
                FontSize = 16, MinHeight = 44 };
            translate.Click += (_, _) => TranslatePage(region);
            var done = new Button { Content = "✕ Done", FontSize = 16,
                MinHeight = 44 };
            done.Click += (_, _) => Close();
            row.Children.Add(copyAll);
            row.Children.Add(translate);
            row.Children.Add(done);
            var (sx, sy, sw, sh) = Windowing.MonitorHelper.PrimaryScreenPhysical();
            Canvas.SetLeft(row, sx + 24);
            Canvas.SetTop(row, sy + sh - 70);
            _canvas.Children.Add(row);
            EverywhereApp.Log("ocr words rendered");
        });
    }

    private static void AddWordBox(string text, double x, double y,
        double w, double h)
    {
        if (_wordsCanvas is null) return;
        var btn = new Button
        {
            Content = text,
            FontSize = 14,
            Padding = new Thickness(2),
            Background = new SolidColorBrush(
                Windows.UI.Color.FromArgb(80, 245, 158, 11)),
        };
        btn.Width = Math.Max(w, 24);
        btn.Height = Math.Max(h, 20);
        btn.Click += (_, _) => CopyText(text);
        Canvas.SetLeft(btn, x);
        Canvas.SetTop(btn, y);
        _wordsCanvas.Children.Add(btn);
    }

    private static void CopyText(string text)
    {
        try
        {
            var dp = new Windows.ApplicationModel.DataTransfer.DataPackage();
            dp.SetText(text);
            Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(dp);
            EverywhereApp.Log("ocr word copied");
        }
        catch (Exception) { }
    }

    private static async void TranslatePage(PixelRect region)
    {
        // Re-OCR with translation (the broker translates the whole page text).
        var png = System.IO.Path.Combine(System.IO.Path.GetTempPath(),
            $"toustovac-ocr-page-{Guid.NewGuid():N}.png");
        try
        {
            var bitmap = Capture.RegionCapturer.CaptureRegion(region);
            if (bitmap is null) return;
            using (var stream = new Windows.Storage.Streams
                .InMemoryRandomAccessStream())
            {
                var encoder = await Windows.Graphics.Imaging.BitmapEncoder
                    .CreateAsync(Windows.Graphics.Imaging.BitmapEncoder
                        .PngEncoderId, stream);
                encoder.SetSoftwareBitmap(bitmap);
                await encoder.FlushAsync();
                stream.Seek(0);
                var reader = new Windows.Storage.Streams.DataReader(stream);
                await reader.LoadAsync((uint)stream.Size);
                var bytes = new byte[reader.UnconsumedBufferLength];
                reader.ReadBytes(bytes);
                reader.DetachStream();
                await System.IO.File.WriteAllBytesAsync(png, bytes);
            }
            bitmap.Dispose();
            // Target language: the user's UI language (Czech by default).
            var target = "cs";
            var reply = _pipe?.RoundTrip(BuildCmd("recognize",
                ("image_path", png), ("translate_to", target),
                ("region_x", region.X), ("region_y", region.Y)));
            if (reply is null) return;
            if (reply.Value.TryGetProperty("translated_text", out var tr))
            {
                var text = tr.GetString() ?? "";
                _dq?.TryEnqueue(() =>
                {
                    if (_canvas is null) return;
                    var block = new Border
                    {
                        Background = new SolidColorBrush(
                            Windows.UI.Color.FromArgb(230, 24, 24, 27)),
                        Padding = new Thickness(16),
                        Child = new ScrollViewer
                        {
                            Content = new TextBlock
                            {
                                Text = text,
                                FontSize = 18,
                                TextWrapping = TextWrapping.Wrap,
                                Foreground = new SolidColorBrush(
                                    Microsoft.UI.Colors.White),
                            },
                        },
                    };
                    Canvas.SetLeft(block, region.X);
                    Canvas.SetTop(block, region.Y);
                    block.Width = Math.Max(region.Width, 300);
                    block.MaxHeight = Math.Max(region.Height, 200);
                    _canvas.Children.Add(block);
                });
                EverywhereApp.Log("ocr page translated");
            }
        }
        catch (Exception ex)
        {
            EverywhereApp.Log($"ocr translate failed: {ex.GetType().Name}");
        }
        finally
        {
            try { if (System.IO.File.Exists(png)) System.IO.File.Delete(png); }
            catch (Exception) { }
        }
    }

    private static void Close()
    {
        if (EverywhereApp.Hotkeys is not null)
        {
            EverywhereApp.Hotkeys.SessionEscapeHandler = null;
        }
        var win = _window;
        _window = null;
        _canvas = null;
        _wordsCanvas = null;
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
            ["kind"] = "ocr",
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
