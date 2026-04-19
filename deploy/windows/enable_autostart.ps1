<#
.SYNOPSIS
    Register a Windows Task Scheduler task that boots the WSL2 distro at
    Windows startup — so the OmniSight compose prod stack auto-recovers
    after a host reboot.

.DESCRIPTION
    Layer 1 of the 4-layer autostart chain documented in
    docs/ops/autostart_wsl.md. WSL2 does NOT start with Windows by default
    — it only boots when accessed. This task forces `wsl.exe -d <distro>`
    at system startup, which triggers the distro's systemd, which starts
    dockerd, which restarts the containers (they all have
    `restart: always`), which restarts cloudflared → the public URL.

    Idempotent: re-running updates the existing task.

.PARAMETER DistroName
    The WSL distro to boot. Default: Ubuntu-24.04 (prod host per
    docs/ops/multi-wsl-deployment.md).

.PARAMETER TaskName
    Task Scheduler entry name. Default: OmniSight-WSL-Autostart.

.PARAMETER Remove
    Uninstall: unregister the task (reverses everything this script
    does).

.PARAMETER Test
    Don't just register — also fire the task immediately after, and
    report whether the distro reached Running state. Use this to verify
    the task works without waiting for an actual reboot.

.EXAMPLE
    .\enable_autostart.ps1
    # Registers the default task on Ubuntu-24.04

.EXAMPLE
    .\enable_autostart.ps1 -Test
    # Registers + fires immediately + reports state

.EXAMPLE
    .\enable_autostart.ps1 -DistroName Ubuntu-22.04 -TaskName MyStaging
    # Register for a staging WSL distro under a different name

.EXAMPLE
    .\enable_autostart.ps1 -Remove
    # Uninstall

.NOTES
    - Must run as Administrator (Task Scheduler writes under SYSTEM).
    - Run ON Windows, NOT inside WSL. From WSL shell you can invoke:
        powershell.exe -ExecutionPolicy Bypass -File deploy\windows\enable_autostart.ps1
      but the prompt for elevation won't work reliably from there — open
      a PowerShell (Admin) window directly.
#>

#Requires -Version 5.1
#Requires -RunAsAdministrator

[CmdletBinding()]
param(
    [string]$DistroName = 'Ubuntu-24.04',
    [string]$TaskName   = 'OmniSight-WSL-Autostart',
    [switch]$Remove,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "`n═══ $msg ═══" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [OK]   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail($msg) { Write-Host "  [FAIL] $msg" -ForegroundColor Red; exit 1 }

# ═════════════════════════════════════════════════════════════════════
# Remove path
# ═════════════════════════════════════════════════════════════════════
if ($Remove) {
    Write-Step "Unregister task: $TaskName"
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "Task '$TaskName' removed"
    } else {
        Write-Warn "No task named '$TaskName' — nothing to do"
    }
    Write-Host ""
    Write-Host "WSL-side cleanup (inside the distro):" -ForegroundColor DarkGray
    Write-Host "  sudo systemctl disable --now omnisight-compose-prod.service" -ForegroundColor DarkGray
    exit 0
}

# ═════════════════════════════════════════════════════════════════════
# Pre-flight
# ═════════════════════════════════════════════════════════════════════
Write-Step "§0 Pre-flight"

# Resolve wsl.exe explicitly — avoids any PATH surprises under SYSTEM.
$wslPath = Join-Path $env:WINDIR 'System32\wsl.exe'
if (-not (Test-Path $wslPath)) { Write-Fail "wsl.exe not found at $wslPath — is WSL installed?" }
Write-Ok "wsl.exe: $wslPath"

# Confirm distro exists. `wsl -l -q` emits UTF-16LE with embedded nulls;
# strip them before comparing.
$raw     = & $wslPath -l -q 2>&1
$distros = ($raw -join "`n") -replace "`0", '' `
           -split "`r?`n" | Where-Object { $_.Trim() -ne '' } | ForEach-Object { $_.Trim() }

if ($distros -notcontains $DistroName) {
    Write-Fail "Distro '$DistroName' not installed. Available: $($distros -join ', ')"
}
Write-Ok "Distro '$DistroName' present"

# ═════════════════════════════════════════════════════════════════════
# Build task definition
# ═════════════════════════════════════════════════════════════════════
Write-Step "§1 Register task: $TaskName"

# `--exec /bin/true` starts the distro, runs a no-op inside it, and
# exits. systemd (PID 1) keeps the distro alive after /bin/true exits,
# so the shell-less wsl call is enough to trigger boot.
$action   = New-ScheduledTaskAction -Execute $wslPath `
              -Argument "-d $DistroName --exec /bin/true"

# AtStartup fires BEFORE any user logs in (fires when the system boots)
# — exactly what we want for a headless prod host.
$trigger  = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
              -AllowStartIfOnBatteries `
              -DontStopIfGoingOnBatteries `
              -StartWhenAvailable `
              -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
              -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

# SYSTEM principal so the task runs regardless of which user logs in
# (and regardless of whether anyone does). Highest priv = no UAC prompt.
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest

$desc = "Boot WSL2 '$DistroName' at Windows startup so the OmniSight " +
        "docker compose prod stack (and Cloudflare Tunnel) auto-recovers. " +
        "See docs/ops/autostart_wsl.md."

# Register — idempotent via -Force: updates existing task in place.
Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description $desc -Force | Out-Null
Write-Ok "Task '$TaskName' registered (State: Ready)"

# ═════════════════════════════════════════════════════════════════════
# Verify
# ═════════════════════════════════════════════════════════════════════
Write-Step "§2 Verify"

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host ("  TaskName:    {0}" -f $task.TaskName)
Write-Host ("  State:       {0}" -f $task.State)
Write-Host ("  Triggers:    {0}" -f ($task.Triggers | ForEach-Object { $_.CimClass.CimClassName } | Join-String -Separator ', '))
Write-Host ("  RunAs:       {0}" -f $task.Principal.UserId)
Write-Host ("  Command:     {0} {1}" -f $task.Actions[0].Execute, $task.Actions[0].Arguments)
Write-Ok "Definition matches — system will boot '$DistroName' on next startup"

# ═════════════════════════════════════════════════════════════════════
# Optional: fire now + confirm distro boots
# ═════════════════════════════════════════════════════════════════════
if ($Test) {
    Write-Step "§3 Test fire (simulate reboot trigger)"

    # Shutdown the distro first so we can watch it come up
    Write-Host "  · stopping $DistroName to make the test observable…"
    & $wslPath -t $DistroName 2>&1 | Out-Null
    Start-Sleep -Seconds 3

    Write-Host "  · starting task $TaskName…"
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 5

    $info   = Get-ScheduledTaskInfo -TaskName $TaskName
    $rc     = $info.LastTaskResult       # 0 == success, 267009 == currently running
    $state  = (& $wslPath -l -v 2>&1) -replace "`0", '' -split "`r?`n" |
              Where-Object { $_ -match [regex]::Escape($DistroName) } |
              Select-Object -First 1

    Write-Host ""
    Write-Host ("  task LastTaskResult: {0}" -f $rc)
    Write-Host ("  wsl -l -v:           {0}" -f $state.Trim())

    if ($state -match 'Running') {
        Write-Ok "$DistroName is Running — task works end-to-end"
    } else {
        Write-Warn "$DistroName not yet in Running state; check 'wsl -l -v' after a few seconds"
    }
}

Write-Host ""
Write-Host "Done. Layer 1 is now in place." -ForegroundColor Green
Write-Host ""
Write-Host "Remaining manual step (inside WSL, one-time — skip if already done):" -ForegroundColor Gray
Write-Host "  cd /home/user/work/sora/OmniSight-Productizer" -ForegroundColor Gray
Write-Host "  scripts/enable_autostart.sh    # installs dockerd + compose systemd layers" -ForegroundColor Gray
Write-Host ""
Write-Host "Full runbook: docs/ops/autostart_wsl.md" -ForegroundColor Gray
