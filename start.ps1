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

function Test-Port1883 {
    $result = netstat -an 2>$null | Select-String "0\.0\.0\.0:1883\s+0\.0\.0\.0:0\s+LISTENING"
    return ($null -ne $result)
}

function Wait-PortFree {
    for ($i = 0; $i -lt 10; $i++) {
        Start-Sleep -Milliseconds 600
        $bound = netstat -an 2>$null | Select-String ":1883\s"
        if (-not $bound) { return $true }
    }
    return $false
}

try {

    Clear-Host
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "   Smart Inventory System - Startup        " -ForegroundColor White
    Write-Host "============================================" -ForegroundColor Cyan

    # ── 1. Mosquitto ──────────────────────────────────────────────────────────
    Write-Step 1 "Starting Mosquitto MQTT broker"

    if (-not (Test-Path $MOSQUITTO)) {
        Write-Fail "Mosquitto not found at: $MOSQUITTO"
        throw "Mosquitto missing"
    }

    # ── Stop all existing Mosquitto processes ─────────────────────────────────
    $procs = Get-Process -Name "mosquitto" -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Warn "Stopping $($procs.Count) existing Mosquitto process(es)..."
        $procs | Stop-Process -Force
        # Wait for each process to fully exit
        foreach ($p in $procs) {
            try { $p.WaitForExit(5000) | Out-Null } catch {}
        }
        Write-OK "Mosquitto process(es) stopped"
    } else {
        Write-Warn "No existing Mosquitto process running"
    }

    # ── Wait for port 1883 to be free ─────────────────────────────────────────
    Write-Warn "Waiting for port 1883 to be released..."
    $portFree = Wait-PortFree
    if ($portFree) {
        Write-OK "Port 1883 is free"
    } else {
        Write-Warn "Port 1883 still in use — attempting to start anyway"
    }

    # ── Start fresh Mosquitto instance ────────────────────────────────────────
    Start-Process -FilePath $MOSQUITTO -ArgumentList "-c `"$CONF`"" -WindowStyle Normal

    Write-Warn "Waiting for broker to listen on port 1883..."
    $waited = 0
    $ready  = $false
    while ($waited -lt 15) {
        Start-Sleep -Seconds 1
        $waited++
        if (Test-Port1883) { $ready = $true; break }
    }

    if ($ready) {
        Write-OK "Broker ready on port 1883"
    } else {
        throw "Mosquitto did not start within 15 seconds — check the Mosquitto window for errors"
    }

    # ── 2. Flask backend ──────────────────────────────────────────────────────
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

    # Print laptop IPs — useful for filling in config.py broker on new networks
    Write-Host ""
    Write-Host "   Laptop IP addresses (for ESP32 config.py):" -ForegroundColor Gray
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
