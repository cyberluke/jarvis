// Current-user named-pipe client for the Everywhere broker. Raw Win32
// (CreateFile + message read mode) — a .NET NamedPipeClientStream cannot
// reliably connect to the broker's message-mode pipe (Connect timed out), so
// this uses the same Win32 calls the proven Python client uses. One
// short-lived transaction per connection. No TCP anywhere.
using System.Runtime.InteropServices;
using System.Text.Json;
using Toastovac.Everywhere.Host.App;
using Toastovac.Everywhere.Host.Protocol;

namespace Toastovac.Everywhere.Host.Pipe;

public sealed class EverywherePipeClient
{
    public const string PipeName = @"\\.\pipe\toastovac-everywhere-v1";

    private readonly int _timeoutMs;

    public EverywherePipeClient(int timeoutMs = 1500)
    {
        _timeoutMs = timeoutMs;
    }

    /// <summary>Round-trip one message. Null when the pipe is unavailable.</summary>
    public JsonElement? RoundTrip(JsonElement request)
    {
        var handle = CreateFile(PipeName, GENERIC_READ | GENERIC_WRITE, 0,
            IntPtr.Zero, OPEN_EXISTING, 0, IntPtr.Zero);
        if (handle == INVALID_HANDLE_VALUE)
        {
            EverywhereApp.Log($"pipe open failed: 0x{Marshal.GetLastWin32Error():x}");
            return null;
        }
        try
        {
            // Message read mode must match the broker's message-mode pipe.
            uint mode = PIPE_READMODE_MESSAGE;
            if (!SetNamedPipeHandleState(handle, ref mode, IntPtr.Zero,
                    IntPtr.Zero))
            {
                EverywhereApp.Log("pipe set message mode failed");
                return null;
            }
            var frame = Framing.Encode(request);
            if (!WriteFile(handle, frame, (uint)frame.Length, out _, IntPtr.Zero))
            {
                EverywhereApp.Log("pipe write failed");
                return null;
            }
            // The broker writes the whole frame (length + payload) as ONE
            // message. Read the whole message in one go (message mode), then
            // split off the length header. Reading a partial header first
            // fails with ERROR_MORE_DATA, which is why the header-only read
            // returned nothing.
            var buf = new byte[Framing.FrameHeaderBytes + Framing.MaxPayloadBytes];
            if (!ReadFile(handle, buf, (uint)buf.Length, out uint got,
                    IntPtr.Zero) || got < Framing.FrameHeaderBytes)
            {
                EverywhereApp.Log("pipe read failed (no reply)");
                return null;
            }
            var full = new byte[got];
            Array.Copy(buf, full, (int)got);
            return Framing.Decode(full);
        }
        finally
        {
            CloseHandle(handle);
        }
    }

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
}
