# Deployment — self-host on WSL + Cloudflare Tunnel

Single-machine deployment for OmniSight on Windows WSL2, exposed to
the internet via Cloudflare Tunnel against a GoDaddy-registered
domain. No port-forwarding, no public IP, free TLS, automatic
restart on reboot.

## Topology

```
Browser → Cloudflare edge (DNS: omnisight.yourdomain.com)
          │   (GoDaddy NS delegated to Cloudflare)
          ▼
     cloudflared daemon (WSL, outbound-only)
          │
          ▼
   WSL2 Ubuntu (systemd-managed)
     ├─ frontend (Next.js)  :3000
     ├─ backend  (FastAPI) :8000
     ├─ SQLite   data/omnisight.db  (WAL)
     └─ Docker daemon (agent sandboxes — Phase 64)
```

Why this combo:

- **No inbound port, no firewall changes.** Cloudflare Tunnel is
  outbound-only. Windows firewall and router untouched.
- **GoDaddy for registration only.** NS delegated to Cloudflare; DNS
  / WAF / rate limit managed in CF.
- **Free TLS.** CF terminates; `Full (strict)` mode works with the
  tunnel.
- **systemd keeps it up across WSL restarts.** Three small units.

## Prerequisites

1. WSL2 Ubuntu 22.04+ with systemd enabled
   (`/etc/wsl.conf`: `[boot]\nsystemd=true`, then `wsl --shutdown`).
2. Docker Engine (not Docker Desktop):
   `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER`.
3. Python 3.12, Node 20+, `npm ci && npm run build` runs cleanly.
4. `.env` populated — see `.env.example`; at minimum set a
   `OMNISIGHT_DECISION_BEARER` and an LLM provider key.

## 1. Delegate DNS to Cloudflare

1. Cloudflare dashboard → **Add site** → enter your GoDaddy domain.
2. CF prints two nameservers (e.g. `roy.ns.cloudflare.com`).
3. GoDaddy → **Nameservers** → **Enter my own** → paste both.
4. Propagation: 24–48h worst case, usually <1h.
5. In CF → **SSL/TLS** → `Full (strict)` (works with the tunnel).

## 2. Create the tunnel

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o /tmp/cf.deb
sudo dpkg -i /tmp/cf.deb

cloudflared tunnel login                                    # browser auth
cloudflared tunnel create omnisight                         # prints UUID
cloudflared tunnel route dns omnisight omnisight.yourdomain.com
cloudflared tunnel route dns omnisight api.omnisight.yourdomain.com
```

Copy the config template and edit:

```bash
cp deploy/cloudflared/config.yml.example ~/.cloudflared/config.yml
${EDITOR:-nano} ~/.cloudflared/config.yml   # replace UUID + hostnames
cloudflared tunnel --config ~/.cloudflared/config.yml ingress validate
```

## 3. Install systemd units

Three units, one per long-running process. Templates are in
`deploy/systemd/`; they carry `USERNAME` / `USER_HOME` placeholders
to sed.

```bash
WHO=$(id -un)
HOME_DIR=$(echo "$HOME" | sed 's|/home/||')
for f in deploy/systemd/*.service; do
  sed -e "s/USERNAME/$WHO/g" -e "s/USER_HOME/$HOME_DIR/g" "$f" \
    | sudo tee "/etc/systemd/system/$(basename $f)" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now omnisight-backend
sudo systemctl enable --now omnisight-frontend
sudo systemctl enable --now cloudflared
```

Verify:

```bash
systemctl status omnisight-backend omnisight-frontend cloudflared
curl -sf http://localhost:8000/api/v1/health | jq .
curl -sf https://api.omnisight.yourdomain.com/api/v1/health | jq .
```

## 4. Windows auto-start

WSL2 on Windows 11 starts on demand. To make sure the services are up
*before* the user logs in:

- **Recommended (Windows 11):** enable "Auto-proxy" + WSL systemd;
  systemd brings the services up on its own when WSL boots.
- **Belt-and-braces:** Task Scheduler, "At system startup",
  `wsl.exe -d Ubuntu -u root /bin/bash -c "systemctl start omnisight-backend omnisight-frontend cloudflared"`.

## 5. Backups

SQLite with WAL needs the online backup API — `cp` mid-write is not
safe. `scripts/deploy.sh` takes a backup before every restart; add a
daily cron for peace of mind:

```cron
0 3 * * * cd /home/$USER/work/sora/OmniSight-Productizer && \
  sqlite3 data/omnisight.db ".backup 'data/backups/daily-$(date +\%Y\%m\%d).db'"
```

Keep the last 7 days; older can be deleted by a `find -mtime +7`.

### Audit log retention (L1-07)

Phase 53's audit_log is hash-chained and grows unbounded. Rotate
nightly alongside the DB backup:

```cron
# Archive audit rows >90 days old into cold JSONL + prune from DB.
0 3 * * * cd /home/$USER/work/sora/OmniSight-Productizer && \
  python3 scripts/audit_archive.py --days 90

# Verify the archive boundary still matches the live DB's chain.
15 3 * * * cd /home/$USER/work/sora/OmniSight-Productizer && \
  python3 scripts/audit_archive.py --verify
```

Archive files land in `data/audit-archive/audit-YYYYMMDD-HHMMSS.jsonl`.
They are immutable — the `--verify` subcommand proves the live DB's
first post-boundary row still points at the last archived row's hash,
so evidence of tampering surfaces as a chain break.

## Common issues

- **`cloudflared` exits immediately** — usually the credentials-file
  path is wrong. `cloudflared tunnel info <name>` confirms the UUID.
- **Frontend loads but API calls fail** — `NEXT_PUBLIC_API_URL` was
  baked at `npm run build`. Rebuild after changing. It's set in
  `omnisight-frontend.service`.
- **`docker: permission denied`** — your WSL user isn't in the
  `docker` group yet. Log out + back in after `usermod`.
- **WSL restart kills the tunnel** — systemd should bring it back.
  `journalctl -u cloudflared -n 50` if not.
