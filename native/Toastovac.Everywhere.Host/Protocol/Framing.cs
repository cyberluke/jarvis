// Wire codec for the Everywhere pipe: 4-byte big-endian payload length +
// UTF-8 JSON, protocol id "toustovac-everywhere/1". Mirrors the Python
// framing in src/jarvis/everywhere/protocol.py exactly.
using System.Text.Json;

namespace Toastovac.Everywhere.Host.Protocol;

public static class Framing
{
    public const string ProtocolId = "toustovac-everywhere/1";
    public const int FrameHeaderBytes = 4;
    public const int MaxPayloadBytes = 262_144;

    public static byte[] Encode(JsonElement message)
    {
        using var ms = new MemoryStream();
        using (var writer = new Utf8JsonWriter(ms))
        {
            message.WriteTo(writer);
        }
        var payload = ms.ToArray();
        if (payload.Length > MaxPayloadBytes)
        {
            throw new InvalidOperationException("message too large");
        }
        var frame = new byte[FrameHeaderBytes + payload.Length];
        frame[0] = (byte)(payload.Length >>> 24);
        frame[1] = (byte)(payload.Length >>> 16);
        frame[2] = (byte)(payload.Length >>> 8);
        frame[3] = (byte)payload.Length;
        payload.CopyTo(frame, FrameHeaderBytes);
        return frame;
    }

    /// <summary>Parse one full frame. Returns null on any violation.</summary>
    public static JsonElement? Decode(ReadOnlySpan<byte> frame)
    {
        if (frame.Length < FrameHeaderBytes)
        {
            return null;
        }
        var n = (frame[0] << 24) | (frame[1] << 16) | (frame[2] << 8) | frame[3];
        if (n <= 0 || n > MaxPayloadBytes)
        {
            return null;
        }
        if (frame.Length != FrameHeaderBytes + n)
        {
            return null;
        }
        try
        {
            using var doc = JsonDocument.Parse(
                frame.Slice(FrameHeaderBytes, n).ToArray());
            return doc.RootElement.Clone();
        }
        catch (JsonException)
        {
            return null;
        }
    }
}
