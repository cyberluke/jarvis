// Physical-pixel region type for the host. WinRT's projected Int32Rect is
// not part of the App SDK's own winmd, so the host carries its own plain
// rectangle and converts at the GDI/WinRT boundaries.
namespace Toastovac.Everywhere.Host.Capture;

public readonly struct PixelRect
{
    public int X { get; }
    public int Y { get; }
    public int Width { get; }
    public int Height { get; }

    public PixelRect(int x, int y, int width, int height)
    {
        X = x;
        Y = y;
        Width = width;
        Height = height;
    }
}
