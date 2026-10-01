// The Everywhere application: a WinUI 3 Application subclass that owns the
// overlay windows, the global hotkeys (WM_HOTKEY via a window subclass), the
// snapshot/transaction state, and the single persistent pipe channel to the
// Python broker.
//
// Task model: actions are queued, not awaited. The broker replies
// "task_queued" immediately and pushes "task_event" frames as the task
// progresses. Delivery is unobtrusive by design: the user keeps working, the
// queue overlay shows progress, and a finished task opens its result panel on
// click — or immediately when a result panel is already active.
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

public sealed partial class EverywhereApp : Application
{
    /// <summary>The running app instance, so overlay windows (the result
    /// panel's Insert button) can reach the task pipeline.</summary>
    internal static EverywhereApp? Instance { get; private set; }

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
private int _activeRequestSerial;

    // Snapshot capture ladder (§Capture ladder): UI Automation into the
    // focused app can block forever on a hung provider, so the capture runs
    // on a worker with a timeout and a clipboard fallback. Single-flight: one
    // capture at a time; waiters fire with the same result. A permanently hung
    // UIA is skipped after a couple of timeouts (streak) so repeated actions
    // stay snappy instead of paying the timeout every time.
    private const int UiaCaptureTimeoutMs = 4000;
    private const int UiaHangSkipThreshold = 2;
    private bool _captureInFlight;
    private int _uiaHangStreak;
    private string? _pendingCaptureAction;
    private readonly object _captureLock = new();
    private readonly List<Action<Dictionary<string, object?>?>> _captureWaiters
        = new();

    // Task plane: broker task_id -> local mirror (queue overlay renders it).
    private readonly Dictionary<string, TaskEntry> _tasks = new();
    private readonly object _tasksLock = new();
    // The task currently shown in the result panel (Insert targets it).
    private TaskEntry? _activeTask;

    public EverywhereApp()
    {
        // Surface otherwise-silent UI-thread exceptions into the host log.
        // Handled=true keeps the process alive: an unhandled XAML exception
        // would otherwise kill the host with 0xc000027b the moment any overlay
        // render throws (e.g. a missing resource), taking the whole toolbar
        // down with it. The full stack is logged for diagnosis.
        UnhandledException += (_, e) =>
        {
            try
            {
                Log($"UI UNHANDLED {e.Exception?.GetType().Name}: "
                    + e.Exception?.ToString()?.Replace('\r', ' ')
                        .Replace('\n', ' '));
            }
            catch (Exception)
            {
            }
            e.Handled = true;
        };
    }

    protected override void OnLaunched(LaunchActivatedEventArgs args)
    {
        Instance = this;
        _window = new Window { Title = "Toustovač Everywhere" };
        _window.Content = ToolbarWindow.Build(_window, DispatchSlot, Dismiss);
        _hwnd = WinRT.Interop.WindowNative.GetWindowHandle(_window);
        Hotkeys = _hotkeys;
        // Borderless topmost tool window: no caption/min/max/close, no taskbar
        // entry, always above the origin app (§Toolbar behavior).
        Windowing.Chrome.MakeToolOverlay(_window);
        _window.AppWindow.Hide();
        // The low-level hook swallows the chord before the origin app sees it,
        // so Ctrl+Shift+Space cannot clear the active selection.
        _hotkeys.Register(_window, Config.LoadHotkeyTable(),
            slot => _window.DispatcherQueue.TryEnqueue(() => DispatchSlot(slot)));
        // WM_HOTKEY backup: the low-level hook cannot see keyboard input
        // destined for elevated windows (UIPI); the system-level hotkeys
        // registered by HotkeyManager still deliver via this subclass.
        Subclassing.Hook(_hwnd, WmHotkey);
        foreach (var conflict in _hotkeys.Conflicts)
        {
            Log($"HOTKEY_CONFLICT {conflict}");
        }
        Log($"host started; hotkeys registered={_hotkeys.Registered.Count} " +
            $"conflicts={_hotkeys.Conflicts.Count}");
        // Clipboard-fallback diagnostics share the host log (step outcomes
        // only — never clipboard text).
        Capture.ClipboardCapture.Log = Log;
        // Persistent pipe + push channel; the queue overlay is the delivery
        // surface, but it stays HIDDEN until there is something to show
        // (first task event / non-empty subscribed snapshot).
        TaskQueueOverlay.Init(OpenTask, CancelTask, ClearFinishedTasks);
        TaskQueueOverlay.Register(_window);
        _pipe.MessageReceived += OnPipeMessage;
        _pipe.StatusChanged += OnPipeStatus;
        _pipe.Start();

        // Verification hooks: open an overlay window at startup so the
        // render path can be exercised without hotkey input.
        var cmdline = Environment.GetCommandLineArgs();
        if (Array.Exists(cmdline, a => a == "--open-subtitles"))
        {
            SubtitlesOverlay.Toggle(_window, _pipe);
        }
        if (Array.Exists(cmdline, a => a == "--open-coach"))
        {
            CoachOverlay.Toggle(_window, _pipe);
        }
    }

    // ── pipe ──────────────────────────────────────────────────────────
    private void OnPipeStatus(string status)
    {
        // Background thread; marshal UI work.
        if (status == "connected")
        {
            // The subscribed ack carries the full task snapshot; the queue
            // overlay rebuilds from it on reconnect.
        }
    }

    private void OnPipeMessage(JsonElement msg)
    {
        if (!msg.TryGetProperty("kind", out var kindProp)
            || kindProp.ValueKind != JsonValueKind.String)
        {
            return;
        }
        var kind = kindProp.GetString();
        switch (kind)
        {
            case "subscribed":
                OnSubscribed(msg);
                break;
            case "task_event":
                OnTaskEvent(msg);
                break;
            case "task_list":
                break;  // handled via round-trip correlation
            default:
                Log($"pipe message unhandled kind={kind}");
                break;
        }
    }

    private void OnSubscribed(JsonElement msg)
    {
        var tasks = new List<TaskEntry>();
        if (msg.TryGetProperty("tasks", out var tasksProp)
            && tasksProp.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in tasksProp.EnumerateArray())
            {
                tasks.Add(ParseTask(item));
            }
        }
        _window?.DispatcherQueue.TryEnqueue(() =>
        {
            lock (_tasksLock)
            {
                _tasks.Clear();
                foreach (var t in tasks)
                {
                    _tasks[t.TaskId] = t;
                }
            }
            // Lazy overlay: appears only when there is something to show.
            if (tasks.Count > 0)
            {
                TaskQueueOverlay.EnsureShown();
            }
            TaskQueueOverlay.ReplaceAll(tasks);
            Log($"subscribed: tasks={tasks.Count}");
        });
    }

    private void OnTaskEvent(JsonElement msg)
    {
        var task = ParseTask(msg);
        _window?.DispatcherQueue.TryEnqueue(() =>
        {
            lock (_tasksLock)
            {
                if (_tasks.TryGetValue(task.TaskId, out var existing))
                {
                    CopyTask(existing, task);
                    task = existing;
                }
                else
                {
                    _tasks[task.TaskId] = task;
                }
            }
            // Reappear after Dismiss: new task activity re-shows the queue
            // overlay with the full history instead of silently caching.
            if (TaskQueueOverlay.EnsureShown())
            {
                lock (_tasksLock)
                {
                    TaskQueueOverlay.ReplaceAll(_tasks.Values);
                }
            }
            TaskQueueOverlay.AddOrUpdate(task);
            switch (task.State)
            {
                case "completed":
                    OnTaskCompleted(task);
                    break;
                case "failed":
                    OnTaskFailed(task);
                    break;
                case "cancelled":
                    Log($"task cancelled task={task.TaskId} action={task.Action}");
                    break;
                case "running":
                case "streaming":
                case "queued":
                case "retrying":
                    break;
            }
        });
    }

    private static void CopyTask(TaskEntry dst, TaskEntry src)
    {
        dst.State = src.State;
        dst.Model = src.Model;
        dst.Profile = src.Profile;
        dst.TargetLanguage = src.TargetLanguage;
        dst.Failure = src.Failure;
        dst.CacheHit = src.CacheHit;
        dst.Truncated = src.Truncated;
        dst.RetryCount = src.RetryCount;
        dst.QueuedAt = src.QueuedAt;
        dst.StartedAt = src.StartedAt;
        dst.FirstTokenAt = src.FirstTokenAt;
        dst.FinishedAt = src.FinishedAt;
        dst.DurationMs = src.DurationMs;
        dst.FirstTokenMs = src.FirstTokenMs;
        dst.Result = src.Result;
        dst.Preview = src.Preview;
        dst.ResultLength = src.ResultLength;
        dst.Position = src.Position;
    }

    // ── delivery: active overlay vs. queue ────────────────────────────
    private void OnTaskCompleted(TaskEntry task)
    {
        Log($"task completed task={task.TaskId} action={task.Action} "
            + $"len={task.ResultLength} cache={task.CacheHit} "
            + $"ms={task.DurationMs}");
        // 1) A result panel is already showing this task (or any task): render
        //    into the active overlay — the user is waiting on results.
        if (ToolbarWindow.ResultPanelActive)
        {
            ShowResultForTask(task);
            _activeTask = task;
            return;
        }
        // 2) The toolbar session is still pending for this very task: show it
        //    in place, exactly like the old synchronous flow.
        if (ToolbarWindow.Mode == ToolbarWindow.OverlayMode.EverywherePending
            && task.RequestId == _pendingRequestId
            && !ToolbarWindow.IsStale(_activeRequestSerial))
        {
            _activeTask = task;
            if (task.Truncated || task.Result.Length == 0)
            {
                // Truncated/empty event result: pull the full result from the
                // broker before rendering into the pending toolbar.
                _ = Task.Run(async () =>
                {
                    var request = new Dictionary<string, object?>
                    {
                        ["protocol"] = Framing.ProtocolId,
                        ["kind"] = "task_result",
                        ["request_id"] = NewRequestId(),
                        ["task_id"] = task.TaskId,
                    };
                    var reply = await _pipe.RoundTripAsync(
                        JsonSerializer.SerializeToElement(request), 8000);
                    _window?.DispatcherQueue.TryEnqueue(() =>
                    {
                        if (reply is not null
                            && reply.Value.TryGetProperty("result", out var r)
                            && r.ValueKind == JsonValueKind.String)
                        {
                            task.Result = r.GetString() ?? "";
                            task.Truncated = false;
                        }
                        ToolbarWindow.ShowResult(_window, task.Result,
                            task.Capability, action: task.Action,
                            onRetranslate: task.Action == "translate"
                                ? Retranslate : null,
                            onInsert: Insert, onCopy: CopyResult);
                    });
                });
                return;
            }
            ToolbarWindow.ShowResult(_window, task.Result,
                task.Capability, action: task.Action,
                onRetranslate: task.Action == "translate" ? Retranslate : null,
                onInsert: Insert, onCopy: CopyResult);
            return;
        }
        // 3) Otherwise: unobtrusive delivery — queue row + toast.
        TaskQueueOverlay.Notify(task.TaskId,
            $"✓ {task.ActionLabel} done · click to view");
    }

    private void OnTaskFailed(TaskEntry task)
    {
        Log($"task failed task={task.TaskId} action={task.Action} "
            + $"failure={task.Failure}");
        TaskQueueOverlay.Notify(task.TaskId,
            $"✗ {task.ActionLabel} failed ({task.Failure})");
        if (ToolbarWindow.ResultPanelActive)
        {
            ToolbarWindow.ShowError(_window,
                task.Failure ?? FailureCodes.ProviderUnavailable);
            _activeTask = task;
        }
    }

    /// <summary>Re-run a failed task's action against the same snapshot
    /// (same target language / prompt).</summary>
    private void RetryTask(TaskEntry task)
    {
        if (task.SnapshotDict is { } snap)
        {
            _snapshot = snap;
        }
        if (_snapshot.Count == 0)
        {
            Log("retry skipped: no snapshot");
            return;
        }
        Log($"task retry task={task.TaskId} action={task.Action}");
        QueueAction(task.Action,
            task.Action == "translate" ? task.TargetLanguage : null);
    }

    /// <summary>The action round-trip failed (PIPE_DISCONNECTED), but the
    /// request may still have reached the broker before the pipe dropped —
    /// the daemon processes actions independently of the overlay. Re-query
    /// the broker's task list and reconcile: a task carrying our request_id
    /// means the action is live server-side (keep the session pending and
    /// let task_event pushes render the outcome, or render a task that
    /// already finished), while a provably absent request surfaces the
    /// disconnect error.</summary>
    private async Task ReconcilePendingRequestAsync(string requestId)
    {
        for (var attempt = 0; attempt < 4; attempt++)
        {
            await Task.Delay(1000 * (attempt + 1));
            try
            {
                var query = new Dictionary<string, object?>
                {
                    ["protocol"] = Framing.ProtocolId,
                    ["kind"] = "task_list",
                    ["request_id"] = NewRequestId(),
                };
                var reply = await _pipe.RoundTripAsync(
                    JsonSerializer.SerializeToElement(query), 5000);
                if (reply is null)
                {
                    continue;  // pipe still down; retry
                }
                JsonElement? match = null;
                if (reply.Value.TryGetProperty("tasks", out var tasksProp)
                    && tasksProp.ValueKind == JsonValueKind.Array)
                {
                    foreach (var item in tasksProp.EnumerateArray())
                    {
                        if (item.TryGetProperty("request_id", out var ridProp)
                            && ridProp.ValueKind == JsonValueKind.String
                            && ridProp.GetString() == requestId)
                        {
                            match = item;
                            break;
                        }
                    }
                }
                _window?.DispatcherQueue.TryEnqueue(() =>
                {
                    if (match is null)
                    {
                        // The request never reached the broker: the action
                        // was not queued, so the error is truthful.
                        Log(FailureCodes.PipeDisconnected
                            + ": action never queued (reconcile)");
                        if (ToolbarWindow.Mode
                                == ToolbarWindow.OverlayMode.EverywherePending
                            && !ToolbarWindow.IsStale(_activeRequestSerial))
                        {
                            ToolbarWindow.ShowError(_window,
                                FailureCodes.PipeDisconnected);
                        }
                        return;
                    }
                    var task = ParseTask(match.Value);
                    task.Capability = ReplaceCapabilityFromSnapshot();
                    task.SnapshotDict = _snapshot;
                    lock (_tasksLock)
                    {
                        _tasks[task.TaskId] = task;
                    }
                    Log($"task reattached after disconnect task={task.TaskId} "
                        + $"req={requestId} state={task.State}");
                    switch (task.State)
                    {
                        case "completed":
                            OnTaskCompleted(task);
                            break;
                        case "failed":
                            ToolbarWindow.ShowError(_window,
                                string.IsNullOrWhiteSpace(task.Failure)
                                    ? FailureCodes.PipeDisconnected
                                    : task.Failure);
                            break;
                        default:
                            // Queued/running/streaming server-side: keep the
                            // pending spinner; task_event pushes render the
                            // outcome as they arrive.
                            break;
                    }
                });
                return;
            }
            catch (Exception ex)
            {
                Log($"reconcile probe failed: {ex.GetType().Name}: {ex.Message}");
            }
        }
        _window?.DispatcherQueue.TryEnqueue(() =>
        {
            Log(FailureCodes.PipeDisconnected
                + ": broker unreachable (reconcile)");
            if (ToolbarWindow.Mode
                    == ToolbarWindow.OverlayMode.EverywherePending
                && !ToolbarWindow.IsStale(_activeRequestSerial))
            {
                ToolbarWindow.ShowError(_window,
                    FailureCodes.PipeDisconnected);
            }
        });
    }

    /// <summary>The request_id of the action the toolbar is currently waiting
    /// on, so a completed task can render into the pending toolbar.</summary>
    private string _pendingRequestId = "";

    // ── hotkey dispatch ───────────────────────────────────────────────
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
                ToolbarWindow.CloseOverlay(_window, "hotkey-reset");
                // Show the toolbar IMMEDIATELY at the cursor. The snapshot
                // capture is UI-Automation into the focused app and can block
                // indefinitely (a hung provider freezes whatever thread calls
                // it) — running it synchronously here would kill the global
                // hooks with the UI thread. It happens on a worker; the
                // toolbar repositions under the selection when it lands.
                var cursor = CursorAnchor();
                Log($"toolbar slot: snapshot=capturing anchor={cursor} "
                    + $"mode={ToolbarWindow.Mode}");
                ToolbarWindow.ResetToChocobar();
                ToolbarWindow.Mode = ToolbarWindow.OverlayMode.Chocobar;
                ToolbarWindow.ShowAt(_window, cursor);
                // Toolbar session: intercept action letters AND navigation
                // keys while the toolbar is up (it is a non-activating
                // overlay). Arrows/Enter/Esc drive the toolbar via the hook.
                _hotkeys.ToolbarKeyCapture = true;
                _hotkeys.ToolbarNavHandler = vk =>
                {
                    bool handled = false;
                    _window?.DispatcherQueue.TryEnqueue(() =>
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
                    _window?.DispatcherQueue.TryEnqueue(Dismiss);
                    return null;
                };
                CaptureSnapshotAsync(snap =>
                {
                    if (snap is null)
                    {
                        Log(FailureCodes.NoSelection);
                        return;
                    }
                    var anchor = GeometryOf(snap);
                    Log($"toolbar slot: snapshot=captured anchor={anchor} "
                        + $"mode={ToolbarWindow.Mode}");
                    // Reposition under the selection only while the session is
                    // still fresh (the user has not picked an action yet).
                    if (ToolbarWindow.Mode == ToolbarWindow.OverlayMode.Chocobar
                        && !ToolbarWindow.ResultPanelActive)
                    {
                        OnUi(() => ToolbarWindow.ShowAt(_window, anchor));
                    }
                });
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
                // Escape hatch for every overlay window: the subtitles /
                // coach / OCR windows are borderless tool overlays with no
                // caption buttons, so the cancel hotkey is the only way to
                // close one when its content is not interactive.
                SubtitlesOverlay.Close();
                CoachOverlay.Close();
                OcrOverlay.Close();
                Dismiss();
                break;
        }
    }

    private void RunAction(string action, string? targetLanguage = null)
    {
        if (_snapshot.Count == 0)
        {
            // Fresh snapshot needed. The capture is UI-Automation into the
            // focused app and can block indefinitely (a hung provider freezes
            // whatever thread calls it) — never on the UI thread, or the
            // global hooks die with it. The ladder (UIA timeout → clipboard
            // fallback) runs off-thread; the action is sent once a snapshot
            // lands, or a NoSelection error when nothing readable exists.
            Log($"action.capture action={action}");
            // Immediate feedback: flip the toolbar to pending while the
            // capture ladder runs so the click visibly registers.
            ToolbarWindow.ShowPending(_window, action);
            lock (_captureLock)
            {
                if (_captureInFlight && _pendingCaptureAction == action)
                {
                    return;  // same action already queued on the capture
                }
                _pendingCaptureAction = action;
            }
            CaptureSnapshotAsync(snap =>
            {
                lock (_captureLock)
                {
                    _pendingCaptureAction = null;
                }
                if (snap is null)
                {
                    Log(FailureCodes.NoSelection);
                    OnUi(() =>
                        ToolbarWindow.ShowError(_window, FailureCodes.NoSelection));
                    return;
                }
                SendAction(action, targetLanguage);
            });
            return;
        }
        SendAction(action, targetLanguage);
    }

    private void SendAction(string action, string? targetLanguage = null)
    {
        QueueAction(action, targetLanguage);
    }

    private void QueueAction(string action, string? targetLanguage = null)
    {
        var serial = ToolbarWindow.NextGeneration();
        _activeRequestSerial = serial;
        OnUi(() => ToolbarWindow.ShowPending(_window, action));
        var snapshotId = (string?)_snapshot["snapshot_id"] ?? "";
        var capability = (string?)_snapshot["replace_capability"]
            ?? ReplaceCapability.CopyOnly;
        var requestId = NewRequestId();
        _pendingRequestId = requestId;
        Log($"action.queued action={action} req={requestId} snapshot={snapshotId}");
        var request = new Dictionary<string, object?>
        {
            ["protocol"] = Framing.ProtocolId,
            ["kind"] = "action",
            ["request_id"] = requestId,
            ["action"] = action,
            ["snapshot_id"] = snapshotId,
        };
        if (!string.IsNullOrEmpty(targetLanguage))
        {
            request["target_language"] = targetLanguage;
        }
        _ = Task.Run(async () =>
        {
            // The broker's session thread can be GIL-starved by the voice
            // pipeline for many seconds (observed 8-100s): a short timeout
            // would fail actions the broker is about to process. 30s keeps
            // the spinner honest while letting slow-but-alive sessions land.
            var reply = await _pipe.RoundTripAsync(
                JsonSerializer.SerializeToElement(request), 30000);
            _window?.DispatcherQueue.TryEnqueue(() =>
            {
                if (reply is null)
                {
                    // The pipe dropped before the reply arrived, but the
                    // broker may already have queued the action — the daemon
                    // processes it independently of this overlay. Do not
                    // paint an error yet: reconcile against the broker's
                    // task list and only surface PIPE_DISCONNECTED when the
                    // request provably never arrived.
                    Log(FailureCodes.PipeDisconnected
                        + " (reply lost; reconciling…)");
                    _ = ReconcilePendingRequestAsync(requestId);
                    return;
                }
                var root = reply.Value;
                if (root.TryGetProperty("failure", out var failure))
                {
                    var code = failure.GetString()
                        ?? FailureCodes.ProviderUnavailable;
                    Log($"action rejected: {code}");
                    ToolbarWindow.ShowError(_window, code);
                    return;
                }
                var taskId = root.TryGetProperty("task_id", out var tid)
                    ? tid.GetString() ?? "" : "";
                Log($"action accepted task={taskId} action={action}");
                lock (_tasksLock)
                {
                    if (!_tasks.TryGetValue(taskId, out var existing))
                    {
                        existing = new TaskEntry
                        {
                            TaskId = taskId,
                            RequestId = requestId,
                            Action = action,
                            SnapshotId = snapshotId,
                            State = "queued",
                            SourceKind = SourceKinds.UiaText,
                        };
                        _tasks[taskId] = existing;
                    }
                    existing.Capability = capability;
                    existing.SnapshotDict = _snapshot;
                }
            });
        });
    }

    private void CancelTask(string taskId)
    {
        var request = new Dictionary<string, object?>
        {
            ["protocol"] = Framing.ProtocolId,
            ["kind"] = "task_cancel",
            ["request_id"] = NewRequestId(),
            ["task_id"] = taskId,
        };
        _ = _pipe.Send(JsonSerializer.SerializeToElement(request));
        Log($"task cancel requested task={taskId}");
    }

    private void ClearFinishedTasks()
    {
        lock (_tasksLock)
        {
            var finished = _tasks.Values
                .Where(t => t.IsFinished).Select(t => t.TaskId).ToList();
            foreach (var tid in finished)
            {
                _tasks.Remove(tid);
                TaskQueueOverlay.Remove(tid);
            }
            Log($"queue cleared finished={finished.Count}");
        }
    }

    private void OpenTask(string taskId)
    {
        TaskEntry? task;
        lock (_tasksLock)
        {
            _tasks.TryGetValue(taskId, out task);
        }
        if (task is null)
        {
            Log($"open task missing task={taskId}");
            return;
        }
        Log($"open task task={taskId} action={task.Action} state={task.State}");
        // Full result may be truncated in the event; fetch on demand.
        if (task.Truncated || (task.State == "completed" && task.Result.Length == 0))
        {
            _ = Task.Run(async () =>
            {
                var request = new Dictionary<string, object?>
                {
                    ["protocol"] = Framing.ProtocolId,
                    ["kind"] = "task_result",
                    ["request_id"] = NewRequestId(),
                    ["task_id"] = taskId,
                };
                var reply = await _pipe.RoundTripAsync(
                    JsonSerializer.SerializeToElement(request), 8000);
                _window?.DispatcherQueue.TryEnqueue(() =>
                {
                    if (reply is not null
                        && reply.Value.TryGetProperty("result", out var r)
                        && r.ValueKind == JsonValueKind.String)
                    {
                        task.Result = r.GetString() ?? "";
                        task.Truncated = false;
                    }
                    ShowResultForTask(task);
                    _activeTask = task;
                });
            });
            return;
        }
        ShowResultForTask(task);
        _activeTask = task;
    }

    /// <summary>Render a task's full result into the toolbar panel, pulling
    /// the untruncated payload from the broker when the event was truncated
    /// or empty.</summary>
    private void ShowResultForTask(TaskEntry task)
    {
        if (task.Truncated || task.Result.Length == 0)
        {
            _ = Task.Run(async () =>
            {
                var request = new Dictionary<string, object?>
                {
                    ["protocol"] = Framing.ProtocolId,
                    ["kind"] = "task_result",
                    ["request_id"] = NewRequestId(),
                    ["task_id"] = task.TaskId,
                };
                var reply = await _pipe.RoundTripAsync(
                    JsonSerializer.SerializeToElement(request), 8000);
                _window?.DispatcherQueue.TryEnqueue(() =>
                {
                    if (reply is not null
                        && reply.Value.TryGetProperty("result", out var r)
                        && r.ValueKind == JsonValueKind.String)
                    {
                        task.Result = r.GetString() ?? "";
                        task.Truncated = false;
                    }
                    ToolbarWindow.ShowResult(_window, task.Result,
                        task.Capability ?? ReplaceCapability.CopyOnly,
                        action: task.Action,
                        onRetranslate: task.Action == "translate"
                            ? Retranslate : null,
                        onInsert: Insert, onCopy: CopyResult);
                });
            });
            return;
        }
        ToolbarWindow.ShowResult(_window, task.Result,
            task.Capability ?? ReplaceCapability.CopyOnly,
            action: task.Action,
            onRetranslate: task.Action == "translate" ? Retranslate : null,
            onInsert: Insert, onCopy: CopyResult);
    }

    /// <summary>Re-run the translate action with a new target language. The
    /// snapshot is unchanged; only the target language differs.</summary>
    private void Retranslate(string targetLanguage)
    {
        if (_snapshot.Count == 0) return;
        QueueAction("translate", targetLanguage);
    }

    internal void Insert()
    {
        var task = _activeTask;
        if (task is null || task.State != "completed")
        {
            Log("insert skipped: no completed active task");
            return;
        }
        var origin = _originElement;
        var capability = task.Capability
            ?? ReplaceCapabilityFromSnapshot();
        var snapshotToken = TokenOf(_snapshot);
        // Restore focus to the origin target before editing: the toolbar is a
        // non-activating overlay, so the click on Insert must not have moved
        // focus away from the app the user is editing (§Focus). Focus restore
        // and the revalidation are UI-Automation into the origin app and can
        // block on a hung provider — never on the UI thread (a frozen UI
        // thread silently kills the global hotkeys), so both run on a worker
        // and the apply continues on the UI thread.
        _ = Task.Run(() =>
        {
            if (origin is null)
            {
                return false;
            }
            try
            {
                origin.SetFocus();
            }
            catch (Exception)
            {
                // Focus restore is best-effort.
            }
            // Revalidate the origin target (§Context transactions).
            try
            {
                var currentHash = TokenOf(
                    SelectionObserver.Capture(origin));
                return !string.IsNullOrEmpty(snapshotToken)
                    && snapshotToken == currentHash;
            }
            catch (Exception)
            {
                return false;
            }
        }).ContinueWith(prev =>
        {
            var ok = prev.Status == TaskStatus.RanToCompletion && prev.Result;
            _window?.DispatcherQueue.TryEnqueue(() =>
            {
                if (!ok)
                {
                    Log(FailureCodes.TargetChanged);
                    return;
                }
                // Ask the broker to revalidate and apply (typed failure codes).
                var apply = new Dictionary<string, object?>
                {
                    ["protocol"] = Framing.ProtocolId,
                    ["kind"] = "apply",
                    ["request_id"] = NewRequestId(),
                    ["snapshot_id"] = task.SnapshotId,
                    ["task_id"] = task.TaskId,
                    ["current"] = task.SnapshotDict ?? _snapshot,
                };
                _ = Task.Run(async () =>
                {
                    var reply = await _pipe.RoundTripAsync(
                        JsonSerializer.SerializeToElement(apply), 5000);
                    _window?.DispatcherQueue.TryEnqueue(() =>
                    {
                        if (reply is null)
                        {
                            // The replacement outcome is uncertain (transport
                            // failure), but the action itself succeeded and
                            // its result is already on screen. Keep the
                            // result panel — the user can copy or re-apply —
                            // instead of replacing it with a transport error.
                            Log(FailureCodes.PipeDisconnected
                                + " (apply reply lost; result kept)");
                            return;
                        }
                        if (reply.Value.TryGetProperty("failure",
                                out var failure))
                        {
                            var code = failure.GetString()
                                ?? FailureCodes.PipeDisconnected;
                            Log($"apply failed: {code}");
                            ToolbarWindow.ShowError(_window, code);
                            return;
                        }
                        Replacer.Apply(capability, task.Result, origin,
                            restoreClipboard: true);
                        ToolbarWindow.ResultPanelActive = false;
                        Dismiss();
                    });
                });
            });
        });
    }

    private string ReplaceCapabilityFromSnapshot()
        => (string?)_snapshot["replace_capability"]
            ?? ReplaceCapability.CopyOnly;

    /// <summary>Copy the active task's (or last snapshot's) text to the
    /// clipboard. Used by the toolbar result panel's Copy button.</summary>
    internal void CopyResult()
    {
        var task = _activeTask;
        var text = (task is not null && task.State == "completed")
            ? task.Result
            : (string?)_snapshot["text"] ?? "";
        if (string.IsNullOrEmpty(text))
        {
            Log("copy skipped: nothing to copy");
            return;
        }
        try
        {
            var dp = new Windows.ApplicationModel.DataTransfer.DataPackage();
            dp.SetText(text);
            Windows.ApplicationModel.DataTransfer.Clipboard.SetContent(dp);
            Log($"copied {text.Length} chars");
        }
        catch (Exception ex)
        {
            Log($"copy failed: {ex.GetType().Name}");
        }
    }

    /// <summary>Run a UI update: directly when already on the UI thread,
    /// otherwise through the window's dispatcher queue. Never drops silently
    /// — a failed marshal is logged (§Capture ladder delivery).</summary>
    private void OnUi(Action action)
    {
        try
        {
            if (_window is not null && _window.DispatcherQueue.HasThreadAccess)
            {
                action();
                return;
            }
            if (_window?.DispatcherQueue.TryEnqueue(() => action()) != true)
            {
                Log("ui marshal failed: dropped UI update");
            }
        }
        catch (Exception ex)
        {
            Log($"ui marshal error: {ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>Capture the focused app's selection and run ``onDone`` on the
    /// UI thread. Single-flight: while one capture is running, further callers
    /// are queued and receive the same result (no thread pile-up). The capture
    /// itself is UI-Automation into the focused process: a hung UIA provider
    /// would block the calling thread forever, so it runs on a worker with a
    /// timeout and a clipboard fallback (§Capture ladder).</summary>
    private void CaptureSnapshotAsync(
        Action<Dictionary<string, object?>?> onDone)
    {
        lock (_captureLock)
        {
            if (_captureInFlight)
            {
                _captureWaiters.Add(onDone);
                return;
            }
            _captureInFlight = true;
        }
        Task.Run(() => CaptureLadder(onDone));
    }

    /// <summary>Capture ladder, worker thread: (1) UI Automation with a hard
    /// timeout; (2) clipboard fallback (simulated Ctrl+C) when UIA hangs or
    /// the focused app is already known to hang UIA (streak).</summary>
    private void CaptureLadder(
        Action<Dictionary<string, object?>?> onDone)
    {
        try
        {
            CaptureLadderCore(onDone);
        }
        catch (Exception ex)
        {
            // A fault anywhere in the ladder must still release the
            // single-flight slot and the waiters — never leave them queued.
            Log($"capture ladder error: {ex.GetType().Name}: {ex.Message}");
            CompleteCapture(null, null, onDone);
        }
    }

    private void CaptureLadderCore(
        Action<Dictionary<string, object?>?> onDone)
    {
        Dictionary<string, object?>? snap = null;
        System.Windows.Automation.AutomationElement? origin = null;

        if (_uiaHangStreak < UiaHangSkipThreshold)
        {
            // The capture may never return (hung provider): it must not run
            // inline, and the ladder must not wait for it forever. A late
            // completion is simply discarded — the fallback already ran.
            var capture = Task.Run(() =>
            {
                snap = SelectionObserver.CaptureFromFocus();
                if (snap is not null)
                {
                    // May block on a hung provider; stays on this worker.
                    origin = System.Windows.Automation.AutomationElement
                        .FocusedElement;
                    SendSnapshotFrameAsync(snap);
                }
            });
            bool done;
            try
            {
                done = capture.Wait(UiaCaptureTimeoutMs);
            }
            catch (Exception)
            {
                done = false;  // capture faulted — treat like a timeout
            }
            if (done && snap is not null)
            {
                _uiaHangStreak = 0;
                CompleteCapture(snap, origin, onDone);
                return;
            }
            _uiaHangStreak++;
            Log($"capture uia=timeout streak={_uiaHangStreak} "
                + $"fg={ForegroundInfo()} ms={UiaCaptureTimeoutMs}");
        }
        else
        {
            Log($"capture uia=skipped streak={_uiaHangStreak} "
                + $"fg={ForegroundInfo()}");
        }

        // (2) Clipboard fallback: copy the focused app's selection, read the
        // clipboard, restore the previous contents. Never for consoles.
        var text = Capture.ClipboardCapture.TryCopySelection();
        if (string.IsNullOrEmpty(text))
        {
            Log("capture fallback=clipboard failed");
            CompleteCapture(null, null, onDone);
            return;
        }
        Log($"capture fallback=clipboard ok len={text.Length}");
        snap = BuildClipboardSnapshot(text);
        SendSnapshotFrameAsync(snap);
        CompleteCapture(snap, null, onDone);
    }

    /// <summary>Publish the capture result: store it, then run the primary
    /// callback and every queued waiter — on the UI thread when possible,
    /// otherwise inline (the callbacks are UI-safe). The delivery is logged so
    /// a dropped queue can never silently eat an action again.</summary>
    private void CompleteCapture(Dictionary<string, object?>? snap,
        System.Windows.Automation.AutomationElement? origin,
        Action<Dictionary<string, object?>?> onDone)
    {
        List<Action<Dictionary<string, object?>?>>? waiters;
        lock (_captureLock)
        {
            _captureInFlight = false;
            waiters = _captureWaiters.Count > 0
                ? new List<Action<Dictionary<string, object?>?>>(
                    _captureWaiters)
                : null;
            _captureWaiters.Clear();
        }
        void RunAll()
        {
            _snapshot = snap ?? new Dictionary<string, object?>();
            if (origin is not null)
            {
                _originElement = origin;
            }
            try
            {
                onDone(snap);
            }
            catch (Exception ex)
            {
                Log($"snapshot callback error: {ex.Message}");
            }
            if (waiters is not null)
            {
                foreach (var waiter in waiters)
                {
                    try
                    {
                        waiter(snap);
                    }
                    catch (Exception ex)
                    {
                        Log($"snapshot waiter error: {ex.Message}");
                    }
                }
            }
        }
        bool enqueued = false;
        try
        {
            enqueued = _window?.DispatcherQueue.TryEnqueue(RunAll) ?? false;
        }
        catch (Exception ex)
        {
            Log($"capture complete enqueue error: {ex.GetType().Name}: {ex.Message}");
        }
        if (!enqueued)
        {
            // The UI queue is unavailable (window teardown or a stalled
            // dispatcher). The callbacks are UI-safe and the action is sent
            // on a worker, so the work still completes instead of spinning.
            Log("capture complete: ui queue unavailable — running inline");
            RunAll();
        }
    }

    /// <summary>Build a broker-valid snapshot from clipboard-copied text.
    /// Provenance stays honest: ``provider_id=clipboard`` marks the source
    /// (``source_kind`` must be a broker-known kind, so ``uia-text`` is used
    /// with the clipboard provider id). CopyOnly capability: the result can be
    /// copied, never auto-inserted into a target we cannot revalidate.</summary>
    private static Dictionary<string, object?> BuildClipboardSnapshot(
        string text)
    {
        var hwnd = GetForegroundWindow();
        uint pid = 0;
        GetWindowThreadProcessId(hwnd, out pid);
        var processName = "";
        try
        {
            processName = System.Diagnostics.Process
                .GetProcessById((int)pid).ProcessName;
        }
        catch (Exception)
        {
        }
        var title = new System.Text.StringBuilder(256);
        GetWindowText(hwnd, title, title.Capacity);
        return new Dictionary<string, object?>
        {
            ["snapshot_id"] = $"u{Guid.NewGuid():N}"[..17],
            ["revision"] = 1,
            ["captured_at"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()
                / 1000.0,
            ["source_kind"] = SourceKinds.UiaText,
            ["provider_id"] = "clipboard",
            ["text"] = text,
            ["text_hash"] = Automation.SelectionObserver.Hash(text),
            ["hwnd"] = (long)hwnd,
            ["process_id"] = (long)pid,
            ["process_name"] = processName,
            ["window_title"] = title.ToString(),
            ["editable"] = false,
            ["replace_capability"] = ReplaceCapability.CopyOnly,
            ["selection_bounds"] = new List<double[]>(),
            ["semantic_context"] = new Dictionary<string, object?>
            {
                ["control_type"] = 0,
            },
            ["provider_token"] = new Dictionary<string, object?>
            {
                ["automation_id"] = "",
                ["control_type"] = 0,
                ["selected_text_hash"] = Automation.SelectionObserver
                    .Hash(text),
                ["is_password"] = false,
                ["source"] = "clipboard-fallback",
            },
        };
    }

    /// <summary>Foreground window identity for diagnostics (name, pid, title —
    /// never selection text).</summary>
    private static string ForegroundInfo()
    {
        var hwnd = GetForegroundWindow();
        if (hwnd == IntPtr.Zero)
        {
            return "none";
        }
        GetWindowThreadProcessId(hwnd, out var pid);
        var name = "";
        try
        {
            name = System.Diagnostics.Process.GetProcessById((int)pid)
                .ProcessName;
        }
        catch (Exception)
        {
        }
        var title = new System.Text.StringBuilder(256);
        GetWindowText(hwnd, title, title.Capacity);
        return $"{name} pid={pid} title={title}";
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(
        IntPtr hWnd, out uint lpdwProcessId);

    [System.Runtime.InteropServices.DllImport("user32.dll",
        CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr hWnd,
        System.Text.StringBuilder lpString, int nMaxCount);

    /// <summary>Register the captured snapshot with the broker (fire and
    /// forget; the reply carries only hashes the host already knows).</summary>
    private void SendSnapshotFrameAsync(Dictionary<string, object?> snapshot)
    {
        try
        {
            var request = new Dictionary<string, object?>
            {
                ["protocol"] = Framing.ProtocolId,
                ["kind"] = "snapshot",
                ["request_id"] = NewRequestId(),
                ["snapshot"] = snapshot,
            };
            _ = _pipe.Send(JsonSerializer.SerializeToElement(request));
        }
        catch (Exception)
        {
        }
    }

    /// <summary>Fallback toolbar anchor: the mouse cursor. Used so the
    /// toolbar can appear without waiting for the (potentially slow or hung)
    /// UIA snapshot; it is repositioned under the selection when the capture
    /// lands.</summary>
    private static (double x, double y)? CursorAnchor()
    {
        try
        {
            GetCursorPos(out var pt);
            return ((double)pt.x, (double)pt.y);
        }
        catch (Exception)
        {
            return null;
        }
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
        // Leaving the toolbar session: stop intercepting letters and reset
        // overlay. Running tasks keep running — they land in the queue.
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
                $"{System.DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} {line}\n");
        }
        catch (Exception)
        {
            // Logging must never throw.
        }
    }

    /// <summary>Parse one task_event / subscribed payload into a TaskEntry.</summary>
    private static TaskEntry ParseTask(JsonElement msg)
    {
        string S(string name) =>
            msg.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.String
                ? p.GetString() ?? "" : "";
        int I(string name) =>
            msg.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.Number
                ? p.GetInt32() : 0;
        double D(string name) =>
            msg.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.Number
                ? p.GetDouble() : 0;
        double? DN(string name) =>
            msg.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.Number
                ? p.GetDouble() : null;
        bool B(string name) =>
            msg.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.True;
        return new TaskEntry
        {
            TaskId = S("task_id"),
            RequestId = S("request_id"),
            Action = S("action"),
            SnapshotId = S("snapshot_id"),
            State = S("state"),
            Model = S("model"),
            Profile = S("profile"),
            TargetLanguage = S("target_language"),
            Failure = S("failure"),
            CacheHit = B("cache_hit"),
            Truncated = B("truncated"),
            RetryCount = I("retry_count"),
            QueuedAt = D("queued_at"),
            StartedAt = DN("started_at"),
            FirstTokenAt = DN("first_token_at"),
            FinishedAt = DN("finished_at"),
            DurationMs = msg.TryGetProperty("duration_ms", out var dm)
                && dm.ValueKind == JsonValueKind.Number ? dm.GetInt32() : null,
            FirstTokenMs = msg.TryGetProperty("first_token_ms", out var fm)
                && fm.ValueKind == JsonValueKind.Number ? fm.GetInt32() : null,
            Result = S("result"),
            Preview = S("preview"),
            ResultLength = I("result_length"),
            Position = I("position"),
            SourceKind = S("source_kind"),
            ProcessName = S("process_name"),
        };
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
