# start.ps1 — Launches Mosquitto, Flask backend, and ESP32 in one go
# Run by double-clicking start.bat

$ErrorActionPreference = "Stop"

$MOSQUITTO = "C:\Program Files (x86)\Mosquitto\mosquitto.exe"
$MPREMOTE  = "c:\Users\yying\Documents\FYP2\esptoolenv\Scripts\mpremote.exe"
$CONF      = Join-Path $PSScriptRoot "mosquitto.conf"
$BACKEND   = Join-Path $PSScriptRoot "backend"

function Write-Step($n, $msg) {
    Write-Host ""
    Write-Host "[$n/3] $msg" -ForegroundColor Cyan
}
function Write-OK($msg)   { Write-Host "      OK  $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "      >>  $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "      !!  $msg" -ForegroundColor Red }

# TCP connection test — the only reliable way to know a port is live
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
    Write-Host "   Smart Inventory System - Startup        " -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Cyan

    # ── 1. Mosquitto ─────────────────────────────────────────────────────────
    Write-Step 1 "Starting Mosquitto MQTT broker"

    if (-not (Test-Path $MOSQUITTO)) {
        Write-Fail "Mosquitto not found at: $MOSQUITTO"
        throw "Mosquitto missing — install from https://mosquitto.org/download/"
    }

    # Stop every existing Mosquitto process and wait for it to die
    $procs = Get-Process -Name "mosquitto" -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Warn "Stopping $($procs.Count) existing Mosquitto process(es)..."
        $procs | Stop-Process -Force
        foreach ($p in $procs) {
            try { $p.WaitForExit(6000) | Out-Null } catch {}
        }
        # Extra wait so the OS releases the port binding
        Start-Sleep -Seconds 1
        Write-OK "Previous Mosquitto stopped"
    }

    # Confirm port 1883 is free before starting (retry up to 5s)
    $portWaited = 0
    while ((Test-Port 1883) -and $portWaited -lt 5) {
        Start-Sleep -Seconds 1
        $portWaited++
    }
    if (Test-Port 1883) {
        Write-Warn "Port 1883 still in use after stop — attempting to start anyway"
    }

    # Launch Mosquitto and keep a reference so we can detect crashes
    $mosqProc = Start-Process -FilePath $MOSQUITTO `
                              -ArgumentList "-c", "`"$CONF`"" `
                              -WindowStyle Normal `
                              -PassThru

    Write-Warn "Waiting for Mosquitto (PID $($mosqProc.Id)) to bind port 1883..."
    $ready = $false
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1

        if ($mosqProc.HasExited) {
            throw "Mosquitto exited immediately (code $($mosqProc.ExitCode)) — check the config file path or port conflicts"
        }

        if (Test-Port 1883) { $ready = $true; break }
    }

    if ($ready) {
        Write-OK "Broker ready on port 1883"
    } else {
        throw "Mosquitto did not bind port 1883 within 15 seconds"
    }

    # ── 2. Flask backend ──────────────────────────────────────────────────────
    Write-Step 2 "Starting Flask backend"

    Write-Warn "Installing Python dependencies..."
    python -m pip install flask "paho-mqtt>=2.0" -q
    Write-OK "Dependencies ready"

    $backendCmd = "Set-Location '$BACKEND'; python app.py"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd -WindowStyle Normal

    Write-Warn "Waiting for backend on port 5000..."
    $ready = $false
    for ($i = 0; $i -lt 25; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Port 5000) { $ready = $true; break }
    }

    if ($ready) {
        Write-OK "Backend ready at http://localhost:5000"
    } else {
        Write-Warn "Backend taking longer than expected — check the Flask window for errors"
    }

    # ── 3. ESP32 ──────────────────────────────────────────────────────────────
    Write-Step 3 "Resetting ESP32 on COM7"

    if (-not (Test-Path $MPREMOTE)) {
        Write-Warn "mpremote not found, skipping ESP32 reset"
    } else {
        $ErrorActionPreference = "Continue"
        & $MPREMOTE connect COM7 reset 2>&1 | Out-Null
        $ErrorActionPreference = "Stop"
        Write-OK "ESP32 reset — main.py booting"
    }

    # ── Done ──────────────────────────────────────────────────────────────────
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "   All systems running                     " -ForegroundColor White
    Write-Host "   Dashboard -> http://localhost:5000       " -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Green

    Write-Host ""
    Write-Host "   Laptop IP addresses (for ESP32 config.py broker):" -ForegroundColor Gray
    try {
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.PrefixOrigin -ne 'WellKnown' -and $_.IPAddress -ne '127.0.0.1' } |
            ForEach-Object {
                Write-Host "     $($_.InterfaceAlias.PadRight(30)) $($_.IPAddress)" -ForegroundColor Gray
            }
    } catch {
        Write-Host "     (could not enumerate IPs)" -ForegroundColor Gray
    }
    Write-Host ""

    Start-Sleep -Seconds 1
    Start-Process "http://localhost:5000"

} catch {
    Write-Host ""
    Write-Fail "STARTUP FAILED: $_"
    Write-Host ""
}

Read-Host "Press Enter to close this window"
