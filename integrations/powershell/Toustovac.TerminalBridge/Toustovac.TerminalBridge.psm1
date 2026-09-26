# Toustovac.TerminalBridge — first-party beacon module (K3, L4-audited).
# Bounded, versioned JSON beacons to the current-user named pipe at
# prompt boundaries. Never blocks prompt rendering; no global keyboard
# hook (PSReadLine history + $? / LASTEXITCODE observation only, which
# is PowerShell-local and non-destructive). All handlers removed on
# unload; original prompt restored.

Set-StrictMode -Version Latest

# Explicit dropped/timeout counter for bounded bridge work (M2.1):
# a down daemon must not freeze prompts and must not fail silently.
$script:ToustovacDropped = 0

$_ToustovacSessionId = $null
$_ToustovacPipe = "Toustovac.TerminalBridge.v1"
$_ToustovacOrigPrompt = $null

# M2.4 framing contract (identical semantics to protocol.py / protocol.ts):
# the 4-byte big-endian prefix encodes the PAYLOAD length only; a full
# frame is FRAME_HEADER_BYTES + payload (max 65_540 bytes).
$ToustovacFrameHeaderBytes = 4
$ToustovacMaxPayloadBytes = 65536
$ToustovacMaxFrameBytes = $ToustovacFrameHeaderBytes + $ToustovacMaxPayloadBytes

function Send-ToustovacFrame([byte[]]$bytes) {
    try {
        if ($null -eq $bytes -or $bytes.Length -gt $ToustovacMaxPayloadBytes) { return }
        $len = [System.BitConverter]::GetBytes([uint32]$bytes.Length)
        [array]::Reverse($len)
        $frame = New-Object byte[] ($ToustovacFrameHeaderBytes + $bytes.Length)
        [System.Buffer]::BlockCopy($len, 0, $frame, 0, $ToustovacFrameHeaderBytes)
        [System.Buffer]::BlockCopy($bytes, 0, $frame, $ToustovacFrameHeaderBytes, $bytes.Length)
        $pipe = New-Object System.IO.Pipes.NamedPipeClientStream(
            ".", $_ToustovacPipe,
            [System.IO.Pipes.PipeDirection]::InOut,
            [System.IO.Pipes.PipeOptions]::Asynchronous)
        try {
            $pipe.Connect(50)
            if (-not $pipe.IsConnected) {
                $script:ToustovacDropped++   # bounded-connect timeout
                return
            }
            $pipe.Write($frame, 0, $frame.Length)
            $pipe.Flush()
            # M3.4: accumulate reads until the advertised payload length
            # is complete — a single Read is not guaranteed to return the
            # whole frame. Same 4-byte big-endian PAYLOAD-length prefix as
            # protocol.py / protocol.ts (frame max 65_540 bytes).
            $buf = New-Object byte[] 4096
            $acc = New-Object System.IO.MemoryStream
            $need = -1
            while ($true) {
                $n = $pipe.Read($buf, 0, $buf.Length)
                if ($n -le 0) { break }
                $acc.Write($buf, 0, $n)
                if ($need -lt 0 -and $acc.Length -ge $ToustovacFrameHeaderBytes) {
                    $hb = $acc.ToArray()
                    $need = ([uint32]$hb[0] -shl 24) -bor ([uint32]$hb[1] -shl 16) -bor ([uint32]$hb[2] -shl 8) -bor [uint32]$hb[3]
                }
                if ($need -ge 0 -and $acc.Length -ge ($ToustovacFrameHeaderBytes + $need)) { break }
                if ($acc.Length -gt $ToustovacMaxFrameBytes) { break }
            }
            $acc.Dispose()
        } finally {
            $pipe.Dispose()
        }
    } catch {
        $script:ToustovacDropped++
    }
}

function Send-ToustovacBeacon {
    param(
        $PreviousSuccess,
        $PreviousNativeExitCode
    )
    $sid = $_ToustovacSessionId
    if (-not $sid) { return }
    try {
        # M2.1 — status arrives as explicit captured arguments only; this
        # function never rereads $?/$LASTEXITCODE (they may already
        # describe the original prompt invocation, not the user command).
        $toustovacPreviousSuccess = $PreviousSuccess
        $toustovacPreviousNativeExitCode = $PreviousNativeExitCode

        $psVer = $PSVersionTable.PSVersion.ToString()
        $edition = "Core"; if ($PSVersionTable.PSEdition) { $edition = $PSVersionTable.PSEdition }
        $cwd = (Get-Location).Path
        $hostn = $env:COMPUTERNAME; if (-not $hostn) { $hostn = $env:HOSTNAME }
        $wt = $env:WT_SESSION
        $tp = $env:TERM_PROGRAM
        $created = (Get-Process -Id $PID -ErrorAction SilentlyContinue).StartTime
        $createdNs = 0
        if ($created) { $createdNs = [int64]((($created - (Get-Date "1601-01-01")).TotalMilliseconds * 10000)) }
        $nowNs = [int64](((Get-Date).ToUniversalTime() - (Get-Date "1970-01-01")).TotalMilliseconds * 10000)

        # L4/M0.4: accepted line via PowerShell-local history (no global
        # hook). Exit semantics by execution kind, from the SNAPSHOTS:
        #   native    -> numeric native code authoritative
        #   powershell-> captured $? authoritative; numeric stays null
        #   unknown   -> no promotion
        $last = Get-History -Count 1 -ErrorAction SilentlyContinue
        if ($null -ne $last -and $last.CommandLine) {
            $cmdText = [string]$last.CommandLine
            $kind = "unknown"
            if ($cmdText -match '^\s*(?:[A-Za-z]:\\|\d|")' -or $cmdText -match '^\s*\d') {
                $kind = "powershell"
            } elseif ($null -ne $toustovacPreviousNativeExitCode) {
                $kind = "native"
            } elseif ($toustovacPreviousSuccess -is [bool]) {
                $kind = "powershell"
            }
            $rec = [ordered]@{
                protocol       = "toustovac-terminal-bridge/2"
                kind           = "execution_record"
                session_id     = $sid
                command        = $cmdText
                exit_code      = $(if ($kind -eq 'native') { [int]$toustovacPreviousNativeExitCode } else { $null })
                success        = [bool]$toustovacPreviousSuccess
                execution_kind = $kind
                cwd            = $cwd
                timestamp_ns   = $nowNs
            }
            Send-ToustovacFrame ([System.Text.Encoding]::UTF8.GetBytes(($rec | ConvertTo-Json -Compress -Depth 5)))
        }

        $obj = [ordered]@{
            protocol                = "toustovac-terminal-bridge/2"
            kind                    = "terminal_context"
            session_id              = $sid
            pid                     = $PID
            parent_pid              = 0
            process_created_ns      = $createdNs
            shell                   = "pwsh"
            shell_version           = $psVer
            ps_edition              = $edition
            cwd                     = $cwd
            hostname                = $hostn
            term_program            = $tp
            target_os               = "windows"
            transport               = "local"
            shell_integration_ready = $true
            timestamp_ns            = $nowNs
        }
        if ($wt) { $obj["wt_session"] = $wt }

        Send-ToustovacFrame ([System.Text.Encoding]::UTF8.GetBytes(($obj | ConvertTo-Json -Compress -Depth 5)))
    } catch {
        # Beacon failures must never block prompt rendering.
    }
}

function Init-ToustovacTerminalBridge {
    [CmdletBinding()]
    param(
        [string]$PipeName = "Toustovac.TerminalBridge.v1"
    )

    if (-not $_ToustovacSessionId) {
        $_ToustovacSessionId = [guid]::NewGuid().ToString()
    }
    if ($PipeName) { $_ToustovacPipe = $PipeName }

    # Minimal prompt wrapper preserving the user's original prompt.
    # M2.1 order: (1) capture $?, (2) capture $LASTEXITCODE, (3) invoke
    # the original prompt preserving its value, (4) beacon with explicit
    # captured args, (5) return the original prompt value unchanged.
    if (-not $global:_ToustovacPromptWrapped) {
        $script:_ToustovacOrigPrompt = ${function:Prompt}
        $orig = $script:_ToustovacOrigPrompt
        Set-Item function:global:Prompt -Value ({
            $toustovacPreviousSuccess = $?
            $toustovacPreviousNativeExitCode = $global:LASTEXITCODE
            $toustovacRenderedPrompt = if ($orig) { & $orig } else { "PS $PWD> " }
            Send-ToustovacBeacon `
                -PreviousSuccess $toustovacPreviousSuccess `
                -PreviousNativeExitCode $toustovacPreviousNativeExitCode
            $toustovacRenderedPrompt
        }.GetNewClosure())
        $global:_ToustovacPromptWrapped = $true
    }

    # Init-time beacon: snapshot immediately at each call site so the
    # values still belong to whatever ran just before the init call.
    $toustovacInitSuccess = $?
    $toustovacInitNative = $global:LASTEXITCODE
    Send-ToustovacBeacon -PreviousSuccess $toustovacInitSuccess `
        -PreviousNativeExitCode $toustovacInitNative
}

function Remove-ToustovacTerminalBridge {
    $_ToustovacSessionId = $null
    if ($global:_ToustovacPromptWrapped -and $script:_ToustovacOrigPrompt) {
        Set-Item function:global:Prompt -Value $script:_ToustovacOrigPrompt
        $global:_ToustovacPromptWrapped = $false
    }
}
