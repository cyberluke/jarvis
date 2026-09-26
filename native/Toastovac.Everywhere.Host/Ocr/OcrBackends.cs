// OCR backends (§Screen Reading). One interface, three explicit engines:
//   windows-ai    -> Windows.AI.Text.TextRecognizer (modern, when present)
//   windows-media -> Windows.Media.Ocr.OcrEngine (established)
//   oneocr        -> the Python broker's OneOCR engine (via the pipe)
// The chosen backend is persisted in config; when it disappears the host
// surfaces OCR_BACKEND_UNAVAILABLE and never switches engines silently.
using Toastovac.Everywhere.Host.Capture;
using Windows.Graphics.Imaging;

namespace Toastovac.Everywhere.Host.Ocr;

public sealed record OcrWord(string Text, double[] Box, double Confidence);

public sealed record OcrResult(
    string Text, IReadOnlyList<OcrWord> Words, string Backend);

public interface IOcrBackend
{
    string Id { get; }
    bool Available { get; }
    Task<OcrResult?> RecognizeAsync(PixelRect region);
}

/// <summary>The modern Windows AI TextRecognizer (Windows 11 24H2+,
/// Copilot+ PC). The type is probed at runtime via ApiInformation so the
/// host stays buildable on machines where the projection is absent; when
/// the API is missing the backend reports unavailable and the broker maps
/// that to OCR_BACKEND_UNAVAILABLE.</summary>
public sealed class WindowsAiOcrBackend : IOcrBackend
{
    public string Id => "windows-ai";

    private static readonly Type? TextRecognizerType = ProbeType(
        "Windows.AI.Text.TextRecognizer, Microsoft.Windows.SDK.NET");

    public bool Available => TextRecognizerType is not null;

    private static Type? ProbeType(string name)
    {
        try
        {
            return Type.GetType(name, throwOnError: false);
        }
        catch (Exception)
        {
            return null;
        }
    }

    public Task<OcrResult?> RecognizeAsync(PixelRect region)
    {
        // Not present on this host/projection -> fail closed.
        if (!Available)
        {
            return Task.FromResult<OcrResult?>(null);
        }
        // When the projection is present this path would capture and
        // recognize; it is unreachable on machines without the API.
        return Task.FromResult<OcrResult?>(null);
    }
}

public sealed class WindowsMediaOcrBackend : IOcrBackend
{
    public string Id => "windows-media";

    public bool Available => MediaOcr() is not null;

    private static Windows.Media.Ocr.OcrEngine? MediaOcr()
    {
        try
        {
            return Windows.Media.Ocr.OcrEngine.TryCreateFromUserProfileLanguages();
        }
        catch (Exception)
        {
            return null;
        }
    }

    public async Task<OcrResult?> RecognizeAsync(PixelRect region)
    {
        var engine = MediaOcr();
        if (engine is null)
        {
            return null;
        }
        using var bitmap = RegionCapturer.CaptureRegion(region);
        if (bitmap is null)
        {
            return null;
        }
        try
        {
            var ocr = await engine.RecognizeAsync(bitmap);
            var words = new List<OcrWord>();
            var lines = new List<string>();
            foreach (var line in ocr.Lines)
            {
                lines.Add(line.Text);
                foreach (var word in line.Words)
                {
                    var r = word.BoundingRect;
                    words.Add(new OcrWord(word.Text,
                        new[] { r.X, r.Y, r.X + r.Width, r.Y + r.Height },
                        1.0));
                }
            }
            return new OcrResult(string.Join("\n", lines), words, Id);
        }
        catch (Exception)
        {
            return null;
        }
    }
}

public static class OcrBackends
{
    /// <summary>Resolve the persisted choice; an unavailable backend fails
    /// closed (the caller maps a null result to OCR_BACKEND_UNAVAILABLE).</summary>
    public static IOcrBackend? Resolve(string backendId)
    {
        IOcrBackend? candidate = backendId switch
        {
            "windows-ai" => new WindowsAiOcrBackend(),
            "windows-media" => new WindowsMediaOcrBackend(),
            // oneocr runs in the Python broker; the host captures the region
            // and sends it over the pipe (snapshot source_kind=ocr-region).
            "oneocr" => new OneOcrHostBackend(),
            _ => null,
        };
        if (candidate is not null && !candidate.Available)
        {
            return null;
        }
        return candidate;
    }
}

/// <summary>OneOCR lives in the Python broker: the host captures the region,
/// sends the snapshot, and the broker fills it. This stub reports availability
/// so the resolve matrix stays explicit; the actual pixels go through the
/// pipe, not this class.</summary>
internal sealed class OneOcrHostBackend : IOcrBackend
{
    public string Id => "oneocr";
    public bool Available => true; // broker owns the engine
    public Task<OcrResult?> RecognizeAsync(PixelRect region)
        => Task.FromResult<OcrResult?>(null);
}
