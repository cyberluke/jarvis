# Optimized build for the existing env: kill jarvis processes, reuse cached
# native artifacts, PyInstaller onedir, no tests.
Set-Location $PSScriptRoot/..
$env:PYTHONPATH = "$PWD\src"

# Free the mapped .pyd/.dll files so Remove-Item can delete dist\.
Get-Process -Name Jarvis, python, python313 -ErrorAction SilentlyContinue |
    Where-Object { $_.Id -ne $PID } |
    ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 700

# Only the PyInstaller artifacts are stale between runs. The native build
# trees (build\native_audio_engine, build\virtual_mic) are cached inputs the
# spec stages into the bundle, so they survive the clean.
if (Test-Path build\jarvis_desktop) { Remove-Item -LiteralPath build\jarvis_desktop -Recurse -Force }
if (Test-Path dist) { Remove-Item -LiteralPath dist -Recurse -Force }

# The native engine DLL must exist before PyInstaller runs: jarvis_desktop.spec
# stages build\native_audio_engine\{Debug,Release}\jarvis_audio_engine.dll and
# the daemon fails closed (AUDIO_DSP_ERROR) when the bundle lacks it.
# Rebuild only when the cached DLL is gone; otherwise skip cmake entirely.
$aeDir = "$PWD\build\native_audio_engine"
$aeDll = @( @(
    (Join-Path $aeDir 'Debug\jarvis_audio_engine.dll'),
    (Join-Path $aeDir 'Release\jarvis_audio_engine.dll')
) | Where-Object { Test-Path $_ } )

if ($aeDll) {
    Write-Host "Native audio engine cached: $($aeDll[0]) - skipping cmake"
} else {
    if (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
        Write-Host "cmake not on PATH - install cmake (>=3.23) + MSVC Build Tools"
        exit 1
    }
    if (Test-Path (Join-Path $aeDir 'CMakeCache.txt')) {
        # Configured already (e.g. after an interrupted run): incremental build.
        & cmake --build $aeDir --parallel 8
    } else {
        & cmake -S "$PWD\native\audio_engine" -B $aeDir
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & cmake --build $aeDir --parallel 8
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $aeDll = @( @(
        (Join-Path $aeDir 'Debug\jarvis_audio_engine.dll'),
        (Join-Path $aeDir 'Release\jarvis_audio_engine.dll')
    ) | Where-Object { Test-Path $_ } )
    if (-not $aeDll) {
        Write-Host "Native audio engine DLL missing after cmake - bundle would ship without AEC3"
        exit 1
    }
    Write-Host "Native audio engine built: $($aeDll[0])"
}

# Everywhere native host (Toastovac.Everywhere.Host.exe, WinUI 3). Cached
# like the other native artifacts: rebuilt only when the exe is missing.
# Requires the .NET 10 SDK (auto-installed via winget below when absent).
# Staged under build\ (preserved across the dist\ clean) so jarvis_desktop.spec
# can bundle it into the onedir folder.
$evExe = "$PWD\build\everywhere_host\Toastovac.Everywhere.Host.exe"
if (Test-Path -LiteralPath $evExe) {
    Write-Host "Everywhere host cached: $evExe - skipping dotnet build"
} else {
    $dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
    $dotnet10 = $dotnet -and (dotnet --list-sdks 2>$null | Where-Object { $_ -match '^10\.' })
    if (-not $dotnet10) {
        # Fire-and-forget toolchain: install the .NET 10 SDK via winget, then
        # refresh PATH in this process so the build continues unattended.
        Write-Host "dotnet 10 SDK missing - installing via winget"
        & winget install Microsoft.DotNet.SDK.10 --accept-package-agreements `
            --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -ne 0) {
            Write-Host "winget .NET 10 SDK install failed ($LASTEXITCODE)"
            exit $LASTEXITCODE
        }
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + `
            ';' + [System.Environment]::GetEnvironmentVariable('Path','User')
        $dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
        if (-not $dotnet) { Write-Host "dotnet still not on PATH after install"; exit 1 }
    }
    & dotnet publish "$PWD\native\Toastovac.Everywhere.Host\Toastovac.Everywhere.Host.csproj" `
        -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true `
        -o "$PWD\build\everywhere_host"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "Everywhere host built: $evExe"
}

# No test run here: pytest is 1 (skip) or 5 (no tests), both non-zero by design.
& "$PSScriptRoot\..\.venv-openvino-npu\Scripts\python.exe" -W ignore -m PyInstaller --noconfirm jarvis_desktop.spec
$pyi = $LASTEXITCODE
if ($pyi -ne 0) { exit $pyi }

& .\dist\Toastovac\Toastovac.exe --smoke-test
exit $LASTEXITCODE
