# ─────────────────────────────────────────────────────────────────────────────
# deploy.ps1 — install Equipment Monitor on a Windows machine
#
# Usage (run from the cloned repo, as the user who will run the services):
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # once, if needed
#   .\deploy.ps1
#
# Prerequisites:
#   - Python 3.11+  (in PATH)
#   - Docker Desktop (running, with "Start Docker Desktop when you log in" enabled)
#
# Services run as Windows Task Scheduler tasks that start at login and
# auto-restart on failure — no extra service manager needed.
#
# On re-deploy (updates): git pull, then re-run this script.
# ─────────────────────────────────────────────────────────────────────────────
#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$INSTALL_DIR = $PSScriptRoot
$PYTHON      = (Get-Command python -ErrorAction Stop).Source

Write-Host ""
Write-Host "=== Equipment Monitor deploy (Windows) ===" -ForegroundColor Cyan
Write-Host "    Install dir : $INSTALL_DIR"
Write-Host "    Python      : $PYTHON"
Write-Host ""

# ── 1. Python dependencies ────────────────────────────────────────────────────
Write-Host "[1/4] Installing Python dependencies..." -ForegroundColor Yellow
& $PYTHON -m pip install --quiet -r "$INSTALL_DIR\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ── 2. TimescaleDB ────────────────────────────────────────────────────────────
Write-Host "[2/4] Starting TimescaleDB container..." -ForegroundColor Yellow
Push-Location $INSTALL_DIR
docker compose up -d
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed — is Docker Desktop running?" }

Write-Host "      Waiting for database to accept connections..."
$tries = 0
do {
    Start-Sleep -Seconds 2
    $tries++
    docker exec equipment-monitor-db pg_isready -U monitor -d equipment 2>$null | Out-Null
    if ($tries -gt 30) { throw "Database did not become ready after 60 s" }
} until ($LASTEXITCODE -eq 0)
Write-Host "      Ready."
Pop-Location

# ── 3. Database schema ────────────────────────────────────────────────────────
Write-Host "[3/4] Initialising database schema..." -ForegroundColor Yellow
Push-Location $INSTALL_DIR
& $PYTHON db/schema.py
if ($LASTEXITCODE -ne 0) { throw "Schema init failed" }
Pop-Location

# ── 4. Task Scheduler tasks ───────────────────────────────────────────────────
Write-Host "[4/4] Registering startup tasks in Task Scheduler..." -ForegroundColor Yellow

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit   ([TimeSpan]::Zero) `
    -RestartCount         99                 `
    -RestartInterval      (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$tasks = @(
    @{ Name = "EquipmentMonitor-Collector"; Arg = "collector/main.py" },
    @{ Name = "EquipmentMonitor-App";       Arg = "app/main.py"       }
)

foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction `
        -Execute          $PYTHON        `
        -Argument         $t.Arg         `
        -WorkingDirectory $INSTALL_DIR

    # AtLogOn trigger — fires when this user logs in.
    # Docker Desktop also requires a logged-in user, so this is the right trigger.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    Register-ScheduledTask `
        -TaskName   $t.Name   `
        -Action     $action   `
        -Trigger    $trigger  `
        -Settings   $settings `
        -RunLevel   Highest   `
        -Force | Out-Null

    # Kill any existing instance and start fresh
    Stop-ScheduledTask  -TaskName $t.Name -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $t.Name

    Write-Host "      $($t.Name) — registered and started"
}

# ── Done ──────────────────────────────────────────────────────────────────────
$ip = (Get-NetIPAddress -AddressFamily IPv4 |
       Where-Object { $_.IPAddress -notmatch '^127\.' -and $_.PrefixOrigin -ne 'WellKnown' } |
       Select-Object -First 1).IPAddress

Write-Host ""
Write-Host "=== Deploy complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "    Dashboard : http://${ip}:8050" -ForegroundColor White
Write-Host ""
Write-Host "    Check status:"
Write-Host "      Get-ScheduledTask -TaskName 'EquipmentMonitor-*' | Select TaskName,State"
Write-Host ""
Write-Host "    View logs (Task Scheduler writes to Event Log):"
Write-Host "      Get-WinEvent -LogName 'Microsoft-Windows-TaskScheduler/Operational' -MaxEvents 20"
Write-Host ""
Write-Host "    Stop/start a service:"
Write-Host "      Stop-ScheduledTask  -TaskName 'EquipmentMonitor-Collector'"
Write-Host "      Start-ScheduledTask -TaskName 'EquipmentMonitor-Collector'"
Write-Host ""
Write-Host "    To update:"
Write-Host "      git pull; .\deploy.ps1"
Write-Host ""
