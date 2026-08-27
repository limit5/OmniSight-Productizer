# Boot-hardening four-piece (OP-2768)

Remediation kit from the 2026-08-13 incident (backend-a down 7 days; see
`docs/retrospectives/2026-08-13-backend-a-7day-outage.md`). Live on the prod
host since 2026-08-13. **The repo copies are the version-control trail —
merging this change does not touch the runtime** (same doctrine as the
OP-2728 ops-alert-channel, Gerrit #2204). Install/update is the recipe below.

## Components

| piece | files | what it does |
|---|---|---|
| container reconcile | `scripts/omnisight-container-reconcile.py`, `deploy/systemd/omnisight-container-reconcile.{service,timer}` | boot+5min & every 15min: a container running/restarting with **zero networks** (the netless-sandbox signature — WSL unclean shutdown corrupts dockerd's libnetwork store; restart-policy restarts never rebuild endpoints) is force-recreated from its compose labels. Allowlist `omnisight-productizer/staging/dev` only; stateful services (postgres/redis/…) never auto-touched; unhealthy>10min and restart-loops report-only. Any finding ⇒ exit 1 ⇒ JIRA alert. |
| compose-unit guards | `deploy/systemd/omnisight-{prod,staging}-compose.service.d/10-hardening.conf` | a **user** unit cannot order `After=` the **system** `docker.service` (cross-manager names don't resolve) — at boot both units raced the daemon. The drop-in polls `docker info` for up to 120s instead, wires `OnFailure=omnisight-alert@%n.service`, and raises `TimeoutStartSec` to 600 (boot bring-up with registry pulls measured 254s). |
| replica-up alert | `scripts/omnisight-replica-up-check.py`, `deploy/systemd/omnisight-replica-up.{service,timer}` | boot+10min & every 5min: query Prometheus `up{job="omnisight-backend"}`; `up==0` **or a missing series** (target gone is worse) ⇒ exit 1 ⇒ JIRA alert. Expected set via `OMNISIGHT_EXPECTED_UP`, default `omnisight-backend=backend-a:8000,backend-b:8001`. |
| journald retention | `deploy/systemd/journald.conf.d/10-omnisight-retention.conf` | `SystemMaxUse=6G` + `MaxRetentionSec=60day`. The journal previously held ~2 boots; the evidence of why backend-a stopped on 2026-08-06 was destroyed before anyone investigated. |

## Install / update

```sh
# pieces 1+3 (user scope, no sudo)
cp scripts/omnisight-container-reconcile.py scripts/omnisight-replica-up-check.py ~/.local/bin/
chmod 755 ~/.local/bin/omnisight-container-reconcile.py ~/.local/bin/omnisight-replica-up-check.py
cp deploy/systemd/omnisight-container-reconcile.* deploy/systemd/omnisight-replica-up.* ~/.config/systemd/user/
# piece 2 (drop-ins)
mkdir -p ~/.config/systemd/user/omnisight-prod-compose.service.d ~/.config/systemd/user/omnisight-staging-compose.service.d
cp deploy/systemd/omnisight-prod-compose.service.d/10-hardening.conf ~/.config/systemd/user/omnisight-prod-compose.service.d/
cp deploy/systemd/omnisight-staging-compose.service.d/10-hardening.conf ~/.config/systemd/user/omnisight-staging-compose.service.d/
systemctl --user daemon-reload
systemctl --user enable --now omnisight-container-reconcile.timer omnisight-replica-up.timer
# piece 4 (root)
sudo install -D -m 644 deploy/systemd/journald.conf.d/10-omnisight-retention.conf /etc/systemd/journald.conf.d/10-omnisight-retention.conf
sudo systemctl restart systemd-journald
```

## Verify

```sh
systemctl --user list-timers | grep -E 'reconcile|replica-up'      # both scheduled
systemctl --user start omnisight-container-reconcile.service       # journal: "fleet clean"
systemctl --user start omnisight-replica-up.service                # journal: up=1 per replica
systemctl --user show omnisight-prod-compose -p OnFailure          # alert wired
OMNISIGHT_EXPECTED_UP='omnisight-backend=backend-a:8000,ghost:1' \
  ~/.local/bin/omnisight-replica-up-check.py; echo rc=$?           # negative test: rc=1
```

## Operational caveats

- **Never `systemctl --user restart` the compose units** — staging's `ExecStop`
  is `compose down` (full teardown), prod's is `compose stop`. Use `start` only.
- While the staging `.env` still carries the prod Anthropic key (leg-2
  calibration residue), the env-contract guard fails staging-compose at every
  boot **by design** — with the drop-in this now files a JIRA alert instead of
  dying silently. Resolve by minting a staging key or removing the residue.
- The reconciler's alerts (and the drop-ins') depend on the OP-2728 channel:
  `~/.local/bin/omnisight-alert-notify.py` + `omnisight-alert@.service`.
- Ports on this host cross WSL-distro boundaries: see
  `docs/sop/lessons/L-OP-2768-wsl-shared-netns-port-owner.md` before hunting a
  mystery listener.

## Fifth piece — public-endpoint probe + reconciler v2 (OP-2770, 2026-08-27)

Added after the prod cloudflared tunnel sat dead for **three weeks**
(`ai.sora-dev.app` → CF 530 since 08-06) while every local check stayed green:
the tunnel's owner was the *system-scope* `omnisight-compose-prod.service`
(OP-1717, `--profile tunnel`), which failed at the 08-13 boot — and the 08-13
sweep only read `systemctl --user --failed`.

| piece | files | what it does |
|---|---|---|
| public probe | `scripts/omnisight-public-probe.py`, `deploy/systemd/omnisight-public-probe.{service,timer}` | every 5 min GET the real public URL through the CF edge (`OMNISIGHT_PUBLIC_PROBES`, default homepage=200 — `/readyz` is deliberately not public); 3 retries then exit 1 ⇒ JIRA. 530 = tunnel down, 404/502 = origin routing. |
| reconciler v2 | same `omnisight-container-reconcile.py` | two report-only detectors: **exited-but-expected** (allowlisted project + restart-policy always/unless-stopped + non-zero exit + not compose-oneoff + dead >30 min — the cloudflared signature) and **SYSTEM-scope failed units** (ignore-list: `dmesg.service`). |

Install: copy the probe script + units per the recipe above; the reconciler is
the same file. Verify: `systemctl --user start omnisight-public-probe` →
journal shows `ok https://… -> 200`.

Also fixed under OP-2770: `scripts/backup_prod_db.sh` prune pipeline — the
`*.db*` glob never matches in PG mode, `ls` exits 2, and `set -Eeuo pipefail`
killed the script the first night the prune gate opened (backup + prune had
succeeded; only the exit code was poisoned). The pinned prod checkout was
hand-patched the same day; this repo copy is the durable fix.
