# start.ps1 - Launches Mosquitto and Flask backend
# Run by double-clicking start.bat

$ErrorActionPreference = "Stop"

# Mosquitto installs to Program Files on x64 and Program Files (x86) on x86;
# winget uses the x64 path. Probe both, then fall back to PATH.
$MOSQUITTO_CANDIDATES = @(
    "C:\Program Files\mosquitto\mosquitto.exe",
    "C:\Program Files (x86)\Mosquitto\mosquitto.exe"
)
$CONF      = Join-Path $PSScriptRoot "mosquitto.conf"
$BACKEND   = Join-Path $PSScriptRoot "backend"
$VENV      = Join-Path $PSScriptRoot ".venv"
$VENV_PY   = Join-Path $VENV "Scripts\python.exe"

function Write-Step($n, $msg) {
    Write-Host ""
    Write-Host "[$n/2] $msg" -ForegroundColor Cyan
}
function Write-OK($msg)   { Write-Host "        OK  $msg" -ForegroundColor Green  }
function Write-Warn($msg) { Write-Host "        >>  $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "        !!  $msg" -ForegroundColor Red    }

function Test-Port([int]$port) {
    try {
        $client  = New-Object System.Net.Sockets.TcpClient
        $connect = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        $ok      = $connect.AsyncWaitHandle.WaitOne(800, $false)
        if ($ok) { $client.EndConnect($connect) }
        $client.Close()
        return $ok
    } catch {
        return $false
    }
}

try {

    Clear-Host
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "  Smart Inventory System - Startup         " -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Cyan

    # 1. Mosquitto
    Write-Step 1 "Starting Mosquitto MQTT broker"

    $MOSQUITTO = $null
    foreach ($candidate in $MOSQUITTO_CANDIDATES) {
        if (Test-Path $candidate) { $MOSQUITTO = $candidate; break }
    }
    if (-not $MOSQUITTO) {
        $onPath = Get-Command mosquitto.exe -ErrorAction SilentlyContinue
        if ($onPath) { $MOSQUITTO = $onPath.Source }
    }
    if (-not $MOSQUITTO) {
        Write-Fail "Mosquitto not found in any of:"
        foreach ($candidate in $MOSQUITTO_CANDIDATES) { Write-Fail "    $candidate" }
        throw "Mosquitto missing - install it with:  winget install EclipseFoundation.Mosquitto"
    }
    Write-OK "Found broker at $MOSQUITTO"

    $procs = Get-Process -Name "mosquitto" -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Warn "Stopping $($procs.Count) existing Mosquitto process(es)..."
        $procs | Stop-Process -Force
        foreach ($p in $procs) {
            try { $p.WaitForExit(6000) | Out-Null } catch {}
        }
        Start-Sleep -Seconds 1
        Write-OK "Previous Mosquitto stopped"
    }

    $portWaited = 0
    while ((Test-Port 1883) -and $portWaited -lt 5) {
        Start-Sleep -Seconds 1
        $portWaited = $portWaited + 1
    }

    $mosquProc = Start-Process -FilePath $MOSQUITTO `
                               -ArgumentList "-c", "`"$CONF`"" `
                               -WindowStyle Normal `
                               -PassThru

    Write-Warn "Waiting for Mosquitto (PID $($mosquProc.Id)) to bind port 1883..."
    $ready = $false
    $i = 0
    while ($i -lt 15) {
        Start-Sleep -Seconds 1
        if ($mosquProc.HasExited) {
            throw "Mosquitto exited immediately (code $($mosquProc.ExitCode)) - check config or port conflicts"
        }
        if (Test-Port 1883) { $ready = $true; break }
        $i = $i + 1
    }

    if ($ready) {
        Write-OK "Broker ready on port 1883"
    } else {
        throw "Mosquitto did not bind port 1883 within 15 seconds"
    }

    # 2. Flask backend
    Write-Step 2 "Starting Flask backend"

    if (-not (Test-Path $VENV_PY)) {
        if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
            throw "Python not found on PATH - install Python 3.9+ first"
        }
        Write-Warn "Creating virtual environment (.venv)..."
        python -m venv $VENV
        if (-not (Test-Path $VENV_PY)) { throw "Failed to create .venv" }
        Write-OK "Virtual environment created"
    }

    Write-Warn "Installing Python dependencies into .venv..."
    $req = Join-Path $PSScriptRoot "requirements-pc.txt"
    $prevEA = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    if (Test-Path $req) {
        & $VENV_PY -m pip install -r $req -q --disable-pip-version-check 2>&1 | Out-Null
    } else {
        & $VENV_PY -m pip install flask "paho-mqtt>=2.0" scikit-learn numpy -q --disable-pip-version-check 2>&1 | Out-Null
    }
    $ErrorActionPreference = $prevEA
    Write-OK "Dependencies ready"

    # ── Local secrets ─────────────────────────────────────────────────────────
    # secrets.local.ps1 is gitignored and sets $env:GEMINI_API_KEY (and friends).
    # Env vars set here are inherited by the backend process spawned below, and
    # app.py reads them at import time — so without this the Assistant tab comes
    # up unavailable even though the key is on the machine. Copy
    # secrets.local.ps1.example to secrets.local.ps1 and fill it in.
    $secretsFile = Join-Path $PSScriptRoot "secrets.local.ps1"
    if (Test-Path $secretsFile) {
        . $secretsFile
        if ($env:GEMINI_API_KEY -or $env:GOOGLE_API_KEY -or $env:ANTHROPIC_API_KEY) {
            Write-OK "Assistant credentials loaded from secrets.local.ps1"
        } else {
            Write-Warn "secrets.local.ps1 set no API key - the Assistant tab will be unavailable"
        }
    } else {
        Write-Warn "No secrets.local.ps1 - the Assistant tab will report itself unavailable."
        Write-Warn "  Copy secrets.local.ps1.example to secrets.local.ps1 and add your key."
    }

    $backendCmd = "Set-Location '" + $BACKEND + "'; & '" + $VENV_PY + "' app.py"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd -WindowStyle Normal

    Write-Warn "Waiting for backend on port 5000..."
    $ready = $false
    $i = 0
    while ($i -lt 25) {
        Start-Sleep -Seconds 1
        if (Test-Port 5000) { $ready = $true; break }
        $i = $i + 1
    }

    if ($ready) {
        Write-OK "Backend ready at http://localhost:5000"
    } else {
        Write-Warn "Backend taking longer than expected - check the Flask window for errors"
    }

    # Done
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  All systems running                      " -ForegroundColor White
    Write-Host "  Dashboard -> http://localhost:5000       " -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Green

    Write-Host ""
    Write-Host "  Laptop IP addresses (for ESP32 config.py broker):" -ForegroundColor Gray
    try {
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.PrefixOrigin -ne 'WellKnown' -and $_.IPAddress -ne '127.0.0.1' } |
            ForEach-Object {
                Write-Host "    $($_.InterfaceAlias.PadRight(30)) $($_.IPAddress)" -ForegroundColor Gray
            }
    } catch {
        Write-Host "    (could not enumerate IPs)" -ForegroundColor Gray
    }
    Write-Host ""

    Write-Host "  ESP32 boards power on automatically - no reset needed." -ForegroundColor Gray
    Write-Host "  All 4 boards connect to WiFi and MQTT on their own." -ForegroundColor Gray
    Write-Host ""

    Start-Sleep -Seconds 1
    Start-Process "http://localhost:5000"

} catch {
    Write-Host ""
    Write-Fail "STARTUP FAILED: $_"
    Write-Host ""
}

Read-Host "Press Enter to close this window"
