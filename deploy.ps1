# ─────────────────────────────────────────────────────────────────────────────
# deploy.ps1 — install / update Equipment Monitor on a Windows machine.
# The full stack (TimescaleDB + collector + Dash app) runs in containers
# via Docker Desktop.  Idempotent — re-run after a `git pull` to update.
#
# Usage (run from the cloned repo, as the user who will run Docker Desktop):
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # once, if needed
#   .\deploy.ps1
#
# Prerequisites:
#   - Docker Desktop (running, with "Start Docker Desktop when you log in"
#     enabled if you want auto-start)
# ─────────────────────────────────────────────────────────────────────────────
#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$INSTALL_DIR = $PSScriptRoot

Write-Host ""
Write-Host "=== Equipment Monitor deploy (Windows) ===" -ForegroundColor Cyan
Write-Host "    Install dir : $INSTALL_DIR"
Write-Host ""

Push-Location $INSTALL_DIR

# ── Pre-flight checks ────────────────────────────────────────────────────────
try {
    docker compose version | Out-Null
} catch {
    throw "Docker / docker compose not found.  Install Docker Desktop first."
}

# ── 1. Migrate from a previous native-Python deploy, if present ──────────────
# Older versions of this repo registered EquipmentMonitor-Collector and
# EquipmentMonitor-App as Task Scheduler tasks that ran `python collector/main.py`
# and `python app/main.py` directly.  They would race the Docker stack for
# port 8050, so stop and unregister them before bringing the containers up.
$existing = Get-ScheduledTask -TaskName "EquipmentMonitor-*" -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[1/3] Detected pre-existing scheduled tasks from a native deploy." -ForegroundColor Yellow
    Write-Host "      Stopping and unregistering so the Docker stack can take over..."
    foreach ($t in $existing) {
        try { Stop-ScheduledTask  -TaskName $t.TaskName -ErrorAction Stop } catch { }
        try { Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false -ErrorAction Stop } catch { }
        Write-Host "      Removed $($t.TaskName)"
    }
} else {
    Write-Host "[1/3] No pre-existing native tasks to clean up." -ForegroundColor Yellow
}

# ── 2. Build images + bring up the full stack ────────────────────────────────
Write-Host "[2/3] Building images and bringing up containers..." -ForegroundColor Yellow
docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed — is Docker Desktop running?"
}

# ── 3. Wait for the dashboard to respond ─────────────────────────────────────
Write-Host "[3/3] Waiting for the dashboard to come up..." -ForegroundColor Yellow
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8050" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
    Write-Host "." -NoNewline
}
Write-Host ""
if (-not $ready) {
    Write-Warning "Dashboard did not respond within 2 minutes.  Check logs:"
    Write-Warning "    docker compose logs -f app"
}

Pop-Location

# ── Summary ──────────────────────────────────────────────────────────────────
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.PrefixOrigin -ne 'WellKnown' } |
       Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "=== Deploy complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "    Dashboard  : http://${ip}:8050" -ForegroundColor White
Write-Host ""
Write-Host "    Status     : docker compose ps"
Write-Host "    App logs   : docker compose logs -f app"
Write-Host "    Collector  : docker compose logs -f collector"
Write-Host "    DB logs    : docker compose logs -f timescaledb"
Write-Host "    Restart    : docker compose restart"
Write-Host "    Update     : git pull; .\deploy.ps1"
Write-Host "    Stop all   : docker compose down       # data persists"
Write-Host "    Wipe DB    : docker compose down -v    # loses all data"
Write-Host ""
