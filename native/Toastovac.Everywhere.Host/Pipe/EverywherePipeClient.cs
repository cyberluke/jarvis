// Persistent current-user named-pipe client for the Everywhere broker.
//
// One long-lived connection: the host subscribes once and the broker pushes
// task_event frames on the same handle, so there is no disconnect-after-write
// race and no per-request CreateFile churn. Requests are correlated by
// request_id into TaskCompletionSources; anything without a pending match
// (pushes, the subscribed ack) surfaces through MessageReceived.
//
// Reconnect: a dead broker (daemon restart, crash) is detected by the read
// loop; the client reconnects with capped backoff, re-subscribes and raises
// StatusChanged so the queue overlay can rebuild from the broker snapshot.
using System.Runtime.InteropServices;
using System.Text.Json;
using Toastovac.Everywhere.Host.App;
using Toastovac.Everywhere.Host.Protocol;

namespace Toastovac.Everywhere.Host.Pipe;

public sealed class EverywherePipeClient
{
    public const string PipeName = @"\\.\pipe\toastovac-everywhere-v1";

    /// <summary>Raised for inbound frames with no pending round-trip
    /// (task_event pushes, subscribed ack, unsolicited replies).</summary>
    public event Action<JsonElement>? MessageReceived;

    /// <summary>Raised on connect/reconnect ("connected") and on loss
    /// ("disconnected"). Always on a background thread.</summary>
    public event Action<string>? StatusChanged;

    private readonly object _writeLock = new();
    private readonly object _pendingLock = new();
    private readonly Dictionary<string, TaskCompletionSource<JsonElement?>>
        _pending = new();
    // Writes are serialized through a dedicated writer thread. A blocked
    // WriteFile (full pipe buffer, orphaned broker session) must never freeze
    // a caller: the health watch, the snapshot ladder and the action round
    // trips all stay responsive and can trigger CancelIoEx + reconnect.
    private readonly Queue<byte[]> _writeQueue = new();
    private readonly ManualResetEventSlim _writeSignal = new(false);
    // The handle is written by the connect thread and read by the health
    // watch and Send() from other threads: a plain IntPtr field can be cached
    // in a register by the JIT (the loops would see a stale INVALID forever).
    // Volatile access via a long backing field keeps every thread honest.
    private long _handleRaw = (long)INVALID_HANDLE_VALUE;
    private IntPtr _handle
    {
        get => (IntPtr)Volatile.Read(ref _handleRaw);
        set => Volatile.Write(ref _handleRaw, (long)value);
    }
    private volatile bool _running;
    private int _reconnectDelayMs = 1000;

    // Session health: the broker's persistent-session thread can be starved
    // (the voice pipeline hogs the daemon GIL, delaying pipe delivery by
    // seconds). A periodic ping proves the session is alive; a missing pong
    // forces a reconnect, which also clears a wedged session. The cycle is
    // deliberately slow: a churning reconnect loop makes a slow broker worse.
    private const int HealthPingIntervalMs = 15000;
    private const int HealthPongTimeoutMs = 45000;
    private long _lastPongTick;

    private long LastPongTick
    {
        get => Volatile.Read(ref _lastPongTick);
        set => Volatile.Write(ref _lastPongTick, value);
    }

    public bool IsConnected => _handle != INVALID_HANDLE_VALUE;

    public void Start()
    {
        if (_running) return;
        _running = true;
        LastPongTick = Environment.TickCount64;
        _ = Task.Run(ConnectLoop);
        _ = Task.Run(HealthWatchLoop);
        _ = Task.Run(WriteLoop);
        // Backstop: if the pipe session goes completely quiet (a frozen host
        // stalls even the health watch), a separate thread cancels the pending
        // I/O and terminates the process. The daemon watchdog respawns a fresh
        // host, which connects with a clean session — no app restart needed.
        _ = Task.Run(SelfWatchdogLoop);
    }

    /// <summary>Dedicated writer: the only thread that ever calls WriteFile.
    /// A blocked write stalls this thread alone; CancelIoEx (from the health
    /// watch or CloseCurrent) releases it.</summary>
    private void WriteLoop()
    {
        while (_running)
        {
            byte[]? frame = null;
            lock (_writeLock)
            {
                if (_writeQueue.Count > 0)
                {
                    frame = _writeQueue.Dequeue();
                }
            }
            if (frame is null)
            {
                // Sleep briefly (or until an enqueue wakes us); a lost signal
                // only costs this wait.
                _writeSignal.Wait(500);
                _writeSignal.Reset();
                continue;
            }
            var handle = _handle;
            if (handle == INVALID_HANDLE_VALUE)
            {
                continue;
            }
            if (!WriteFile(handle, frame, (uint)frame.Length, out _,
                    IntPtr.Zero))
            {
                var err = Marshal.GetLastWin32Error();
                EverywhereApp.Log($"pipe write failed: 0x{err:x}");
                // The connection is dead: force the connect loop to reset.
                CloseCurrent();
                FailAllPending(FailureCodes.PipeDisconnected);
            }
        }
    }

    private void SelfWatchdogLoop()
    {
        const int QuietMs = 60000;
        while (_running)
        {
            Thread.Sleep(5000);
            if (!_running)
            {
                break;
            }
            var idle = Environment.TickCount64 - LastPongTick;
            var handle = _handle;
            if (handle == INVALID_HANDLE_VALUE || idle <= QuietMs)
            {
                continue;
            }
            EverywhereApp.Log(
                $"pipe watchdog: session quiet {idle}ms — self-terminating");
            try
            {
                CancelIoEx(handle, IntPtr.Zero);
            }
            catch (Exception)
            {
            }
            // Give the log a moment to flush, then exit so the daemon's
            // host watchdog respawns us with a fresh session.
            System.Threading.Thread.Sleep(300);
            Environment.Exit(97);
        }
    }

    public void Stop()
    {
        _running = false;
        var h = Interlocked.Exchange(ref _handleRaw, (long)INVALID_HANDLE_VALUE);
        if (h != (long)INVALID_HANDLE_VALUE)
        {
            CloseHandle((IntPtr)h);
        }
        FailAllPending(FailureCodes.PipeDisconnected);
    }

    /// <summary>Send one request and await its correlated reply.</summary>
    public async Task<JsonElement?> RoundTripAsync(JsonElement request,
        int timeoutMs = 8000)
    {
        var rid = GetRequestId(request);
        if (string.IsNullOrEmpty(rid))
        {
            return null;
        }
        var tcs = new TaskCompletionSource<JsonElement?>(
            TaskCreationOptions.RunContinuationsAsynchronously);
        lock (_pendingLock)
        {
            _pending[rid] = tcs;
        }
        if (!Send(request))
        {
            lock (_pendingLock)
            {
                _pending.Remove(rid);
            }
            return null;
        }
        var done = await Task.WhenAny(tcs.Task, Task.Delay(timeoutMs));
        if (done != tcs.Task)
        {
            lock (_pendingLock)
            {
                _pending.Remove(rid);
            }
            EverywhereApp.Log($"pipe round-trip timeout rid={rid}");
            return null;
        }
        return await tcs.Task;
    }

    /// <summary>Blocking round-trip for the legacy overlays (subtitles,
    /// coach, OCR selector). Never call from the UI thread with a slow
    /// request; these commands are answered immediately by the broker.</summary>
    public JsonElement? RoundTrip(JsonElement request, int timeoutMs = 15000)
    {
        try
        {
            return RoundTripAsync(request, timeoutMs).GetAwaiter().GetResult();
        }
        catch (Exception ex)
        {
            EverywhereApp.Log($"pipe round-trip failed: {ex.GetType().Name}");
            return null;
        }
    }

    /// <summary>Fire-and-forget send (snapshot registration etc.). The frame is
    /// queued to the dedicated writer thread — the caller never blocks on the
    /// pipe (§Pipe health: a blocked WriteFile must not freeze callers).</summary>
    public bool Send(JsonElement message)
    {
        if (_handle == INVALID_HANDLE_VALUE)
        {
            return false;
        }
        byte[] frame;
        try
        {
            frame = Framing.Encode(message);
        }
        catch (Exception ex)
        {
            EverywhereApp.Log($"pipe encode failed: {ex.GetType().Name}");
            return false;
        }
        lock (_writeLock)
        {
            _writeQueue.Enqueue(frame);
        }
        _writeSignal.Set();
        return true;
    }

    // ── session health watch ──────────────────────────────────────────
    private void HealthWatchLoop()
    {
        EverywhereApp.Log("pipe health: watch started");
        while (_running)
        {
            try
            {
                Thread.Sleep(HealthPingIntervalMs);
                if (!_running || _handle == INVALID_HANDLE_VALUE)
                {
                    continue;
                }
                var idle = Environment.TickCount64 - LastPongTick;
                if (idle > HealthPongTimeoutMs)
                {
                    // No reply of any kind for too long: the session is stale
                    // (broker-side read stuck). Closing our handle unblocks the
                    // broker's ReadFile; the connect loop reconnects and
                    // re-subscribes.
                    EverywhereApp.Log(
                        $"pipe health: stale session idle={idle}ms — reconnecting");
                    CloseCurrent();
                    LastPongTick = Environment.TickCount64;
                    continue;
                }
                // Probe the session with a ping (round trip: reply proves both
                // directions work).
                var rid = NewRequestId();
                var ping = new Dictionary<string, object?>
                {
                    ["protocol"] = Framing.ProtocolId,
                    ["kind"] = "ping",
                    ["request_id"] = rid,
                };
                EverywhereApp.Log(
                    $"pipe health: ping sent rid={rid} idle={idle}ms");
                _ = RoundTripAsync(JsonSerializer.SerializeToElement(ping), 4000)
                    .ContinueWith(t =>
                    {
                        if (t.Status == TaskStatus.RanToCompletion
                            && t.Result is not null)
                        {
                            LastPongTick = Environment.TickCount64;
                        }
                    });
            }
            catch (Exception ex)
            {
                // The watch must survive any fault — a dead watch silently
                // reintroduces stale sessions.
                EverywhereApp.Log(
                    "pipe health: watch error " + ex.GetType().Name + ": "
                    + ex.Message);
                Thread.Sleep(2000);
            }
        }
    }

    // ── connection loop ───────────────────────────────────────────────
    private void ConnectLoop()
    {
        while (_running)
        {
            if (TryConnect())
            {
                _reconnectDelayMs = 1000;
                LastPongTick = Environment.TickCount64;
                StatusChanged?.Invoke("connected");
                EverywhereApp.Log("pipe connected (subscribed)");
                ReadLoop();
                CloseCurrent();
                StatusChanged?.Invoke("disconnected");
                EverywhereApp.Log("pipe disconnected; reconnecting…");
                FailAllPending(FailureCodes.PipeDisconnected);
            }
            if (!_running)
            {
                break;
            }
            Thread.Sleep(_reconnectDelayMs);
            _reconnectDelayMs = Math.Min(_reconnectDelayMs * 2, 15000);
        }
    }

    private bool TryConnect()
    {
        var handle = CreateFile(PipeName, GENERIC_READ | GENERIC_WRITE, 0,
            IntPtr.Zero, OPEN_EXISTING, 0, IntPtr.Zero);
        if (handle == INVALID_HANDLE_VALUE)
        {
            return false;
        }
        // Message read mode must match the broker's message-mode pipe.
        uint mode = PIPE_READMODE_MESSAGE;
        if (!SetNamedPipeHandleState(handle, ref mode, IntPtr.Zero,
                IntPtr.Zero))
        {
            CloseHandle(handle);
            EverywhereApp.Log("pipe set message mode failed");
            return false;
        }
        _handle = handle;
        // Subscribe: the broker replies "subscribed" and then keeps the
        // connection open for task_event pushes.
        var hello = new Dictionary<string, object?>
        {
            ["protocol"] = Framing.ProtocolId,
            ["kind"] = "subscribe",
            ["request_id"] = NewRequestId(),
        };
        return Send(JsonSerializer.SerializeToElement(hello));
    }

    private void CloseCurrent()
    {
        var h = Interlocked.Exchange(ref _handleRaw, (long)INVALID_HANDLE_VALUE);
        if (h != (long)INVALID_HANDLE_VALUE)
        {
            // Cancel a pending blocking read first so the broker-side session
            // thread is released even when it never saw our frames.
            CancelIoEx((IntPtr)h, IntPtr.Zero);
            CloseHandle((IntPtr)h);
        }
    }

    private void ReadLoop()
    {
        var buf = new byte[Framing.FrameHeaderBytes + Framing.MaxPayloadBytes];
        while (_running)
        {
            var handle = _handle;
            if (handle == INVALID_HANDLE_VALUE)
            {
                return;
            }
            if (!ReadFile(handle, buf, (uint)buf.Length, out uint got,
                    IntPtr.Zero) || got < Framing.FrameHeaderBytes)
            {
                return;  // pipe gone
            }
            LastPongTick = Environment.TickCount64;
            var msg = Framing.Decode(buf.AsSpan(0, (int)got));
            if (msg is null)
            {
                EverywhereApp.Log("pipe frame decode failed");
                continue;
            }
            var value = msg.Value;
            var rid = GetRequestId(value);
            if (!string.IsNullOrEmpty(rid))
            {
                TaskCompletionSource<JsonElement?>? tcs = null;
                lock (_pendingLock)
                {
                    if (_pending.TryGetValue(rid, out tcs))
                    {
                        _pending.Remove(rid);
                    }
                }
                if (tcs is not null)
                {
                    tcs.TrySetResult(value);
                    continue;
                }
            }
            MessageReceived?.Invoke(value);
        }
    }

    private void FailAllPending(string failure)
    {
        List<TaskCompletionSource<JsonElement?>> pending;
        lock (_pendingLock)
        {
            pending = new List<TaskCompletionSource<JsonElement?>>(
                _pending.Values);
            _pending.Clear();
        }
        foreach (var tcs in pending)
        {
            tcs.TrySetResult(null);
        }
    }

    private static string? GetRequestId(JsonElement value)
    {
        if (value.TryGetProperty("request_id", out var rid)
            && rid.ValueKind == JsonValueKind.String)
        {
            return rid.GetString();
        }
        return null;
    }

    private static string NewRequestId()
        => Guid.NewGuid().ToString("N")[..12];

    private const uint GENERIC_READ = 0x80000000;
    private const uint GENERIC_WRITE = 0x40000000;
    private const uint OPEN_EXISTING = 3;
    private const uint PIPE_READMODE_MESSAGE = 0x2;
    private static readonly IntPtr INVALID_HANDLE_VALUE = new(-1);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFile(string name, uint access,
        uint share, IntPtr security, uint creation, uint flags,
        IntPtr templateFile);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetNamedPipeHandleState(IntPtr handle,
        ref uint mode, IntPtr maxCollectionCount, IntPtr collectDataTimeout);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool WriteFile(IntPtr handle, byte[] buffer,
        uint bytesToWrite, out uint bytesWritten, IntPtr overlapped);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ReadFile(IntPtr handle, byte[] buffer,
        uint bytesToRead, out uint bytesRead, IntPtr overlapped);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CancelIoEx(IntPtr hFile, IntPtr lpOverlapped);
}