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

try {

    Clear-Host
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "   Smart Inventory System - Startup        " -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Cyan

    # ── 1. Mosquitto ──────────────────────────────────────────────────────
    Write-Step 1 "Starting Mosquitto MQTT broker"

    function Test-Port1883 {
        $result = netstat -an | Select-String "0.0.0.0:1883\s+0.0.0.0:0\s+LISTENING"
        return ($null -ne $result)
    }

    if (-not (Test-Path $MOSQUITTO)) {
        Write-Fail "Mosquitto not found at: $MOSQUITTO"
        throw "Mosquitto missing"
    }

    # Always stop any existing Mosquitto processes to avoid stale/zombie brokers
    Write-Warn "Stopping any existing Mosquitto processes..."
    Get-Process -Name "mosquitto" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 1

    Start-Process -FilePath $MOSQUITTO -ArgumentList "-c `"$CONF`"" -WindowStyle Normal

    Write-Warn "Waiting for broker..."
    $waited = 0
    while ($waited -lt 10) {
        Start-Sleep -Seconds 1
        $waited++
        if (Test-Port1883) { break }
    }

    if (-not (Test-Port1883)) {
        throw "Mosquitto did not start within 10 seconds"
    }
    Write-OK "Broker ready on port 1883"

    # ── 2. Flask backend ──────────────────────────────────────────────────
    Write-Step 2 "Starting Flask backend"

    Write-Warn "Installing Python dependencies..."
    python -m pip install flask "paho-mqtt>=2.0" -q
    Write-OK "Dependencies ready"

    $backendCmd = "Set-Location '$BACKEND'; python app.py"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd -WindowStyle Normal

    Write-Warn "Waiting for backend on port 5000..."
    $waited = 0
    $ready  = $false
    while ($waited -lt 25) {
        Start-Sleep -Seconds 1
        $waited++
        try {
            $null = Invoke-WebRequest -Uri "http://localhost:5000/api/status" `
                                      -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
            $ready = $true
            break
        } catch { }
    }

    if ($ready) {
        Write-OK "Backend ready at http://localhost:5000"
    } else {
        Write-Warn "Backend taking longer than expected - check the Flask window for errors"
    }

    # ── 3. ESP32 ──────────────────────────────────────────────────────────
    Write-Step 3 "Resetting ESP32 on COM7"

    if (-not (Test-Path $MPREMOTE)) {
        Write-Warn "mpremote not found, skipping ESP32 reset"
    } else {
        $ErrorActionPreference = "Continue"
        & $MPREMOTE connect COM7 reset 2>&1 | Out-Null
        $ErrorActionPreference = "Stop"
        Write-OK "ESP32 reset - main.py booting"
    }

    # ── Done ──────────────────────────────────────────────────────────────
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "   All systems running                     " -ForegroundColor White
    Write-Host "   Dashboard -> http://localhost:5000       " -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Green
    Write-Host ""

    Start-Sleep -Seconds 1
    Start-Process "http://localhost:5000"

} catch {
    Write-Host ""
    Write-Fail "STARTUP FAILED: $_"
    Write-Host ""
}

Read-Host "Press Enter to close this window"
