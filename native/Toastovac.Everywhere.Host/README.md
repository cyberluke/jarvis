# Toastovac.Everywhere.Host

Native Windows companion process for the Toustovač Everywhere plane.

- .NET 10 + WinUI 3 on Windows App SDK 2.5.1.
- Owns: global hotkeys, UI Automation selection observation, overlay
  positioning (per-monitor DPI, work-area clamping), Alt+drag region OCR,
  text replacement, input injection.
- Talks to the Python Jarvis runtime only through the current-user named
  pipe `\\.\pipe\toastovac-everywhere-v1` (4-byte big-endian length prefix +
  UTF-8 JSON, protocol `toustovac-everywhere/1`). No TCP.
- The Python broker (`src/jarvis/everywhere/`) owns prompts, model routing,
  streaming, the prompt library and the transaction state machine.

Layout:

```text
App/          Program.cs, EverywhereApp.cs, ToolbarWindow.cs
Protocol/     Framing.cs, Messages.cs
Pipe/         EverywherePipeClient.cs
Automation/   SelectionObserver.cs
Capture/      RegionCapturer.cs
Input/        HotkeyManager.cs
Ocr/          OcrBackends.cs
Replacement/  Replacer.cs
Security/     Boundaries.cs
Windowing/    MonitorHelper.cs
```

Build: `dotnet build Toastovac.Everywhere.Host.csproj` (needs the .NET 10
SDK and the Windows App SDK 2.5.1 NuGet feed). Run next to the daemon: the
daemon starts the pipe, the host connects on demand.

Hotkey collisions from `RegisterHotKey` are surfaced as `HOTKEY_CONFLICT`
in the log with the slot name; existing Toastovač shortcuts keep priority.
