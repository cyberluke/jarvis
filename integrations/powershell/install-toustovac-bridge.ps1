# Idempotent installer/remover for the Toustovac terminal bridge module.
# Adds ONE marked import block to the user's PowerShell profile; never
# rewrites the whole profile; timestamped backup; atomic replace.

[CmdletBinding()]
param(
    [string]$Action = "install"   # install | uninstall | status
)

$Block = @(
    "# >>> TOUSTOVAC TERMINAL INTEGRATION >>>"
    'Import-Module "$env:LOCALAPPDATA\Toustovac\TerminalBridge\Toustovac.TerminalBridge.psd1"'
    "# <<< TOUSTOVAC TERMINAL INTEGRATION <<<"
)

function Get-ProfilePath {
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        return $PROFILE.CurrentUserCurrentHost
    }
    return $PROFILE
}

$path = Get-ProfilePath
Write-Host "Profile path: $path"

if ($Action -eq "status") {
    if (Test-Path $path) {
        $c = Get-Content $path -Raw
        if ($c -match 'TOUSTOVAC TERMINAL INTEGRATION') {
            Write-Host "Installed (marked block present)."
        } else { Write-Host "Not installed." }
    } else { Write-Host "No profile file." }
    return
}

$dir = Split-Path -Parent $path
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }

if ($Action -eq "install") {
    $existing = if (Test-Path $path) { Get-Content $path -Raw } else { "" }
    if ($existing -match 'TOUSTOVAC TERMINAL INTEGRATION') {
        Write-Host "Already installed (block present)."
        return
    }
    if ($existing) {
        $stamp = Get-Date -Format "yyyyMMddHHmmss"
        Copy-Item $path "$path.bak.$stamp"
    }
    $tmp = "$path.tmp"
    $lines = @()
    if ($existing) { $lines += $existing.TrimEnd() }
    $lines += ""
    $lines += $Block
    Set-Content -Path $tmp -Value $lines -Encoding UTF8
    Move-Item -Path $tmp -Destination $path -Force
    Write-Host "Toustovac terminal bridge block installed."
    return
}

if ($Action -eq "uninstall") {
    if (-not (Test-Path $path)) { Write-Host "No profile file."; return }
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    Copy-Item $path "$path.bak.$stamp"
    $c = Get-Content $path
    $out = New-Object System.Collections.Generic.List[string]
    $skip = $false
    foreach ($line in $c) {
        if ($line -eq '# >>> TOUSTOVAC TERMINAL INTEGRATION >>>') { $skip = $true; continue }
        if ($skip) {
            if ($line -eq '# <<< TOUSTOVAC TERMINAL INTEGRATION <<<') { $skip = $false }
            continue
        }
        $out.Add($line)
    }
    $tmp = "$path.tmp"
    Set-Content -Path $tmp -Value $out -Encoding UTF8
    Move-Item -Path $tmp -Destination $path -Force
    Write-Host "Toustovac terminal bridge block removed."
    return
}

Write-Host "Unknown action: $Action"
