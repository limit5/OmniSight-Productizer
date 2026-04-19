# Auto-Start on WSL Host Reboot

Goal: after a Windows host reboot, the full OmniSight prod stack (including
Cloudflare Tunnel) is back online within ~1–2 minutes without any human
action.

This is four layers. Layers 2–4 are scripted and live inside WSL;
layer 1 must be set up once in Windows. The Task Scheduler task is
created **once**, not re-run per reboot.

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 1 — Windows boots → WSL2 distro (Ubuntu-24.04) starts │  ← Task Scheduler
│   ↓                                                         │
│ Layer 2 — WSL systemd → docker.service (dockerd) starts     │  ← systemctl enable docker
│   ↓                                                         │
│ Layer 3 — dockerd → containers with restart=always resume   │  ← already set
│   ↓                                                         │
│ Layer 4 — (safety net) systemd → `compose up -d` if any     │  ← systemd unit
│            container was `compose down`'d before reboot     │
└─────────────────────────────────────────────────────────────┘
```

---

## Layer 1 — Windows: boot the WSL distro at system startup

WSL2 does **not** auto-start with Windows by default. It only starts when
something (a shell, a file explorer path, a command) accesses it. We
create a one-off Task Scheduler task that triggers `wsl.exe` at boot.

### Option A — PowerShell script (recommended)

From a **PowerShell window running as Administrator**:

```powershell
cd C:\path\to\OmniSight-Productizer   # adjust to your actual WSL-mounted path
powershell -ExecutionPolicy Bypass -File deploy\windows\enable_autostart.ps1
```

That script is committed at `deploy/windows/enable_autostart.ps1` and does
exactly the same work the manual recipe below describes, with these
extras:
- checks the distro actually exists before touching Task Scheduler
- `-Test` flag fires the task immediately and verifies the distro reaches
  Running state — skip the real reboot while still proving it works
- `-Remove` flag cleanly unregisters the task
- `-DistroName` / `-TaskName` parameters if you ever use this on a
  different WSL distro or want a different label

Example verification run:

```powershell
deploy\windows\enable_autostart.ps1 -Test
```

### Option B — Manual PowerShell recipe (audit trail for §A)

If you prefer inline, or want to review what the script does before
running it, paste this into **PowerShell (Admin)**:

```powershell
$action = New-ScheduledTaskAction -Execute "wsl.exe" `
  -Argument "-d Ubuntu-24.04 --exec /bin/true"
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest
Register-ScheduledTask -TaskName "OmniSight-WSL-Autostart" `
  -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
  -Description "Boot Ubuntu-24.04 at Windows startup so OmniSight stack auto-recovers"
```

What it does:
- `wsl.exe -d Ubuntu-24.04 --exec /bin/true` starts the distro (runs `/bin/true`
  inside, which exits immediately). The distro stays running because
  systemd (as PID 1) holds it alive.
- Runs as SYSTEM so it fires before any user logs in.
- `-AllowStartIfOnBatteries` so laptops don't skip it on battery.

### Option C — GUI (Task Scheduler)

1. Start → type `Task Scheduler` → open
2. Action: **Create Task…** (not "Basic Task" — we need more options)
3. **General** tab:
   - Name: `OmniSight-WSL-Autostart`
   - "Run whether user is logged on or not"
   - "Run with highest privileges"
   - Configure for: Windows 10 / 11
4. **Triggers** tab → New → Begin the task: **At startup** → OK
5. **Actions** tab → New:
   - Action: Start a program
   - Program/script: `wsl.exe`
   - Add arguments: `-d Ubuntu-24.04 --exec /bin/true`
6. **Conditions** tab: uncheck "Start the task only if the computer is on AC power"
7. OK → provide admin credentials when prompted

### Verify the task exists

```powershell
Get-ScheduledTask -TaskName OmniSight-WSL-Autostart | Format-List State, TaskName
```

Should print `State: Ready`.

---

## Layers 2–4 — WSL: dockerd + systemd unit

Run once inside Ubuntu-24.04:

```bash
cd /home/user/work/sora/OmniSight-Productizer
scripts/enable_autostart.sh
```

That script:
1. `sudo systemctl enable docker` — layer 2 (dockerd auto-starts on WSL boot)
2. Installs `/etc/systemd/system/omnisight-compose-prod.service` — layer 4
   (runs `docker compose --profile tunnel up -d` if containers aren't running)
3. `sudo systemctl enable --now omnisight-compose-prod.service`
4. Checks that every container has `RestartPolicy=always` — layer 3 sanity

It is idempotent — safe to re-run after config changes.

### Verify

```bash
# Each answer should be "enabled"
systemctl is-enabled docker
systemctl is-enabled omnisight-compose-prod.service
```

---

## End-to-end test (destructive — only do when OK with stack bouncing)

```powershell
# From Windows PowerShell (Admin) — simulate a WSL reboot
wsl.exe --shutdown
Start-Sleep 10

# Fire the Task Scheduler task manually (same as what boot will do)
Start-ScheduledTask -TaskName OmniSight-WSL-Autostart
Start-Sleep 30

# From WSL — stack should be back up
wsl.exe -d Ubuntu-24.04 --exec docker ps
```

Expected: 5 containers running (backend-a, backend-b, caddy, frontend, cloudflared).

For a full Windows-boot test, actually reboot the host.

---

## Recovery time budget

Typical cold-start after Windows reboot:

| Step | Typical |
|---|---|
| Windows boot → login screen | 15–60 s |
| Task Scheduler fires → WSL2 starts | 5–15 s |
| WSL systemd + dockerd ready | 10–20 s |
| `compose up -d` returns (images cached) | 5–10 s |
| Containers healthy (backend lifespan + /readyz) | 15–40 s |
| cloudflared QUIC → CF edge registered | 2–5 s |
| **Total (https://ai.sora-dev.app live again)** | **~1 to 2 min** |

First-ever boot (no image cache) adds ~5 min to pull images — rare.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Stack down after Windows reboot, `wsl -l -v` shows `Stopped` | Layer 1 missing | Re-run PowerShell one-liner in §1 Option A |
| `systemctl is-enabled docker` → `disabled` | Layer 2 missing | `sudo systemctl enable docker` |
| Containers stopped, `docker ps -a` shows `Exited` | `docker compose down` was run manually and layer 4 didn't fire | `sudo systemctl start omnisight-compose-prod.service` |
| Layer 4 service is `failed` with "daemon not running" | `/run/docker.sock` not ready when unit started | Inspect `journalctl -u omnisight-compose-prod` — usually transient, `systemctl restart` fixes |
| CF Tunnel shows `down` even after stack healthy | cloudflared container running but can't resolve `frontend` | Check Public Hostname URL in CF dashboard = `frontend:3000` (see deploy post-mortem) |

---

## Turning autostart OFF

If you ever want to stop auto-recovery (e.g. for maintenance):

```bash
# WSL side
sudo systemctl disable --now omnisight-compose-prod.service
# (leave docker enabled unless you want dockerd down too)
```

```powershell
# Windows side — one-shot via the committed script:
deploy\windows\enable_autostart.ps1 -Remove

# Or by hand (equivalent):
Disable-ScheduledTask -TaskName OmniSight-WSL-Autostart
# or hard-remove:
Unregister-ScheduledTask -TaskName OmniSight-WSL-Autostart -Confirm:$false
```
