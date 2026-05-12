# Staging Migration Runbook — 5a → 5c (AUDIT-19d / OP-974)

**Owner:** Deploy on-call · **Spec:** META AUDIT-19 · **Ticket:** OP-974
· **BlockedBy:** AUDIT-19c (OP-973) · **Blocks:** AUDIT-19 META completion

This runbook moves the **staging stack** off the co-tenanted **5a** host
(same machine as prod, isolated only by Compose project name + cgroup
quota — see [`staging-environment-runbook.md`](staging-environment-runbook.md))
onto a **dedicated 5c box**:

> **5c** = Windows 11 host running **WSL2 Ubuntu-24.04** with **Docker
> Engine** inside WSL, on the **same LAN segment** as the prod host.

It is written so a single operator can run it **in a weekend**: a Friday
evening cut-over, a Saturday soak, a Sunday "keep or roll back" decision.

It assumes Phase 1 (5a) is stable — i.e. AUDIT-19a/b/c have shipped *and*
their host bring-up DoDs are done (compose unit enabled, snapshot timer
enabled, sync timer enabled, OP-965 gate timers enabled). If 5a is not
yet stable, **stop** — stabilise 5a first; migrating an unstable stack
just moves the instability.

What this runbook does **not** cover:

* Building the 5c box itself (Windows install, WSL2 enablement, Docker
  Engine install) — that is the OP-927 provisioning ticket. This runbook
  starts from "WSL2 Ubuntu-24.04 with Docker is up and I can `ssh` into
  it".
* The Phase 2 DB upgrade (6c SQL anonymizer → 6g Greenmask). That is a
  separate ticket; this runbook migrates the **6c** pipeline as-is (the
  anonymizer is a one-file seam, so the swap is orthogonal to the host
  move — see [`project_staging_gate_architecture.md` §Q6]).
* The Phase 2 canary upgrade (7d dummy-green → 7c read-only mirror). Also
  a separate ticket; this runbook keeps whatever canary producer is wired
  at migration time.

Related runbooks / docs:

* [`staging-environment-runbook.md`](staging-environment-runbook.md) — the
  5a systemd-wrapped stack (what we are migrating *from*).
* [`staging-environment.md`](staging-environment.md) — OP-767 stack
  topology + `main_promoted` auto-deploy worker.
* [`staging-runbook.md`](staging-runbook.md) — OP-878 blue-green
  `scripts/staging_deploy.sh` flow + Caddy ingress switch.
* `project_staging_gate_architecture.md` (memory) §Migration prep — the
  portability checklist this runbook implements.

---

## 0. What moves, and what stays

| Lives in git (moves with a `git clone`/`git pull` on 5c) | Lives on the host (must be re-created or copied on 5c) |
|---|---|
| `deploy/staging/docker-compose.yml`, `deploy/staging/caddy.json` | `deploy/staging/.env` + `deploy/staging/.env.local` (secrets — `.gitignored`) |
| `deploy/systemd/omnisight-staging-compose.service` | `~/.config/systemd/user/*.{service,timer}` (the installed copies) |
| `deploy/systemd/staging-sync.{service,timer}` | `~/.config/omnisight/*.env` (release-audit DSN, per-host overrides, chmod 600) |
| `deploy/systemd/staging-pg-snapshot.{service,timer}` | `~/.config/omnisight/gerrit-claude-bot-ed25519` (bridge SSH key — only if the `bridge` profile is used) |
| `deploy/systemd/staging-gate-{canary,smoke}.{service,timer}`, `staging-gate-alert.service` | Docker named volumes (`staging-postgres`, `staging-artifacts`, `staging-sdks`, `staging-caddy-data`, `staging-caddy-config`) |
| `infra/staging/verify-env-contract.sh`, `anonymize.sh`, `anonymize-fields.yaml`, `snapshot-restore.sh` | `~/work/sora/logs/...` log directories (the units `append:` to them) |
| `infra/staging/.env.template` (placeholders only) | Linger flag (`loginctl enable-linger "$USER"`) |
| `scripts/sync_staging_to_develop.sh`, `scripts/staging_deploy.sh`, `scripts/auto_deploy_staging.py` | DNS record + firewall rule (LAN — see §5) |

**Volume strategy.** All staging state is in **Docker named volumes**, not
bind mounts to a fixed host path (the one bind mount,
`${STAGING_SANITIZED_BACKUP_DIR:-/var/lib/omnisight/staging/backups}`, is a
read-only mount of a path that is *re-created* by the snapshot pipeline,
not migrated). That means the migration of *data* is one of:

1. **Cleanest (recommended): don't migrate data at all.** Staging is
   reconstructible — `scripts/sync_staging_to_develop.sh` re-deploys the
   develop tip; `infra/staging/snapshot-restore.sh` re-loads an anonymized
   prod snapshot. Stand the 5c stack up empty, let the timers refill it.
   The only thing you lose is the current `_prev` rollback DB, which is one
   snapshot cycle old anyway.
2. **If you want a warm start:** `docker run --rm -v staging-postgres:/v -v
   "$PWD":/out alpine tar czf /out/staging-postgres.tgz -C /v .` on 5a,
   `scp` it, restore the mirror image on 5c. Only worth it for
   `staging-postgres` (the artifact/sdk/caddy volumes refill themselves).

The path-portability audit (§1) is what guarantees option 1 works without
hand-editing anything except the host-prefix in the systemd units.

---

## 1. Portability audit (AC #1)

Before cutting over, prove the 5a artifacts are host-portable. This is the
DoD gate — **the audit must return 0 hits**.

### 1.1 Run it

The audit is encoded as a pytest so it runs in CI on every change to the
staging artifacts (it cannot silently rot):

```bash
pytest -q tests/test_staging_migration_5a_to_5c.py -k portability
```

It scans `infra/staging/`, `deploy/staging/`, and the staging
`deploy/systemd/*` units for the four anti-patterns below and asserts none
are present. A green run == "0 hits" == this AC item satisfied.

> **Why a test, not a `scripts/portability-audit.sh`?** The original
> ticket sketched a standalone shell script under `scripts/`. `scripts/`
> is `area:tooling`; this ticket is scoped to `area:docs` + `area:tests`.
> The audit logic landed as a `tests/` module instead — same coverage,
> CI-enforced, and in-area. If a future change *does* want a CLI wrapper,
> it is a thin `scripts/` shim over the same assertions (one `area:tooling`
> follow-up, not a blocker for this migration).

### 1.2 What the audit checks (and the current verdict)

| # | Check | Rule | 5a verdict |
|---|---|---|---|
| 1 | **Paths relative or via env var** | No `bind` mount or config path in `docker-compose.yml`/`caddy.json` may be an absolute host path *unless* it is `${VAR:-default}` form. Volumes must be **named** (not host-path bind mounts). | ✅ `caddy.json` mounted `./caddy.json`; the one absolute mount is `${STAGING_SANITIZED_BACKUP_DIR:-...}` (env var); the bridge SSH key is `${STAGING_GERRIT_SSH_KEY_FILE:-...}` (env var, profile-gated); all five data volumes are named. |
| 2 | **No hard-coded `localhost:<port>` outside `.env.local`** | The literal `host:port` of any staging service must come from an env var or live only in `deploy/staging/.env.local`. Specifically: no `localhost:6432` / `127.0.0.1:6432` (pgbouncer) anywhere in tracked files; the `localhost:9000` health URL in the compose unit must be a `${...}` default that the host env file can override. | ✅ No pgbouncer port anywhere (no pgbouncer in the staging topology). `localhost:9000` appears only as the **default** of `OMNISIGHT_STAGING_HEALTHZ_URL`, overridable via `~/.config/omnisight/staging-compose.env`. In-container healthchecks (`localhost:8000/readyz` etc.) are the container's *own* port — correct, not a host coupling. |
| 3 | **All ports via env** | Every `ports:` mapping in `docker-compose.yml` must be `"${STAGING_*_PORT:-default}:container"`, never a bare literal host port. | ✅ `STAGING_POSTGRES_PORT`, `STAGING_BACKEND_A_PORT`, `STAGING_BACKEND_B_PORT`, `STAGING_HTTP_PORT`, `STAGING_HTTPS_PORT`, `STAGING_FRONTEND_PORT` — all templated with defaults. |
| 4 | **Cgroup limits as fractions (auto-scale with host)** | The *aggregate* ceiling — the systemd `omnisight-staging-compose.service` `MemoryMax`/`CPUQuota` — must be a **percentage** (`NN%`), so it re-derives from the new box's RAM/CPU automatically. | ✅ `MemoryMax=30%`, `CPUQuota=30%`, `IOWeight=50` (a relative weight, not a byte count). |

### 1.3 By-design items the audit does **not** flag (and why)

These look like portability hits but are intentional; the audit
whitelists them and this runbook handles them in §3:

* **Absolute paths in the systemd `[Service]` sections** —
  `WorkingDirectory=/home/user/sora-bridge`, `ExecStart=/usr/bin/bash
  /home/user/sora-bridge/...`, `EnvironmentFile=-/home/user/.config/...`,
  `StandardOutput=append:/home/user/work/sora/logs/...`. systemd
  *requires* `ExecStart` to be an absolute path — there is no relative
  form. The migration handles this with one `sed` pass (§3 step 5) that
  rewrites the repo prefix to wherever the operator cloned on 5c. The
  audit asserts these prefixes are **uniform** (a single
  `OMNISIGHT_REPO_PREFIX` worth of editing), not that they are absent.
* **Per-container `mem_limit: 1g|2g|512m|128m` in `docker-compose.yml`** —
  these are *workload* ceilings (a backend process needs ~2 GB regardless
  of how big the box is), not host fractions. Docker Compose has no
  "percent of host RAM" form for `mem_limit`. On 5c (a dedicated box, so
  more headroom than the 30%-of-shared on 5a) they stay the same and
  simply leave more slack. The *aggregate* auto-scaling lever is the
  systemd `MemoryMax=30%` (check #4), which is the one that matters.
* **`STAGING_PG_HOST=127.0.0.1`, `STAGING_PG_PORT=55432` defaults in
  `snapshot-restore.sh` / `staging-pg-snapshot.service`** — these are the
  *loopback* address of the staging Postgres' published port *on the
  staging host*. On 5c that is still `127.0.0.1:55432` from inside 5c
  (the snapshot orchestrator runs on the same box as the staging
  Postgres). They are env-var overridable; nothing to change unless you
  publish Postgres on a non-default port via `STAGING_POSTGRES_PORT`.
* **`https://staging.sora.services` defaults** (`OMNISIGHT_STAGING_URL`,
  `STAGING_HOSTNAME`, the frontend build args) — a *DNS name*, not an IP.
  Moving the box is a DNS change (§5), not an artifact change. The whole
  point of using a name here is that it survives the move.

If the audit ever returns a *new* hit (someone adds a bare literal host
port, an absolute bind mount, a non-`%` cgroup limit, or a divergent
systemd path prefix), fixing it is an `area:devops` change on the
offending artifact — file it as a blocker on the migration ticket and
re-run the audit before proceeding.

---

## 2. WSL2 gotchas (AC #2)

5c is the first time the staging stack runs under WSL2. The Linux you get
inside WSL is *almost* a normal Ubuntu, but five things bite if you don't
plan for them. Read all five before step 3.

### 2.1 WSL2 networking — use **mirrored mode** for LAN visibility

By default WSL2 puts your distro behind a NAT on a `vEthernet (WSL)`
adapter: the distro gets a `172.x` address that is **not reachable from
other machines on the LAN**, only from the Windows host (and even then via
a changing IP). The prod host's `release_milestone_checker` /
staging-gate probes need to reach `https://staging.sora.services` *over
the LAN* — NAT mode breaks that.

Fix: enable **mirrored networking mode** (WSL ≥ 2.0.0, Windows 11 22H2+).
In `%USERPROFILE%\.wslconfig` on the Windows host:

```ini
[wsl2]
networkingMode=mirrored
# Let the distro see (and be seen on) the host's LAN interface directly.
dhcp=true
# Optional but recommended on a server box: don't let WSL fiddle with DNS.
dnsTunneling=true
firewallEnabled=true
```

Then `wsl --shutdown` from PowerShell and restart the distro. Verify:
`ip addr` inside WSL should now show the **same** subnet as the Windows
host's LAN NIC, and `hostname -I` should be a LAN address other machines
can reach.

If mirrored mode is unavailable (older WSL), the fallback is a
`netsh interface portproxy` rule on the Windows host forwarding
`0.0.0.0:443` → `<wsl-ip>:8443` plus a Windows Firewall allow rule — but
that re-introduces the changing-`<wsl-ip>` fragility. Upgrade WSL instead.

### 2.2 systemd in WSL2 — supported, but **off by default**

Recent WSL (≥ 0.67.6, shipping in Windows 11) supports running systemd as
PID 1 inside the distro — which is required, because every staging unit
(`omnisight-staging-compose.service`, the timers) is a **systemd user
unit**. It is **not on by default**. In `/etc/wsl.conf` *inside the
distro*:

```ini
[boot]
systemd=true
```

Then `wsl --shutdown` and restart. Verify: `systemctl is-system-running`
should return `running` (or `degraded` — degraded is fine as long as the
units you care about are green) rather than `offline`/failing. Then the
user manager: `systemctl --user status` should work, and
`loginctl enable-linger "$USER"` must be set so the user units survive the
WSL session closing (WSL "logs you out" the moment the last shell exits).

> Without `loginctl enable-linger`, the timers stop the instant you close
> your terminal — the single most common "staging silently stopped
> deploying" cause on WSL.

### 2.3 Filesystem performance — keep everything on the **Linux** filesystem

Files under `/mnt/c/`, `/mnt/d/`, … are the Windows filesystem proxied
over the 9P protocol. It is **an order of magnitude slower** for the kind
of access Docker + a git repo + Postgres do (lots of small stats, fsync).
A `docker build` or `git status` on `/mnt/c/...` can take *minutes*.

Rules:

* Clone the repo into the distro's **own** filesystem, e.g.
  `~/sora-bridge` (which is `\\wsl$\Ubuntu-24.04\home\<user>\...` — ext4,
  fast). **Never** `git clone` into `/mnt/c/Users/...`.
* Docker Engine's data root (`/var/lib/docker`) must be on the Linux fs —
  it is, by default, when you install Docker Engine *inside* WSL (not
  Docker Desktop with the Windows backend). The named volumes
  (`staging-postgres` etc.) then live on ext4 automatically.
* Do **not** use Docker Desktop's WSL integration for the staging box —
  it puts the daemon on the Windows side. Install Docker Engine natively
  in the Ubuntu distro (`apt install docker-ce`), add `$USER` to the
  `docker` group.

### 2.4 Time sync — WSL2 clock drift breaks TLS

WSL2's clock can drift (notably after the Windows host sleeps/hibernates):
the VM clock pauses, Windows resumes, and the WSL clock is now *behind*.
A clock skewed by more than a few minutes makes **TLS handshakes fail**
(`certificate is not yet valid` / `has expired`) — which kills the
staging-gate HTTPS probes, the Caddy ACME renewal, the asyncpg TLS to
Postgres, and `git fetch` over HTTPS in `sync_staging_to_develop.sh`.

Fixes (do both):

* Make sure `systemd-timesyncd` is running inside the distro
  (`systemctl status systemd-timesyncd`) — with `systemd=true` it usually
  is; if not, `apt install systemd-timesyncd` and enable it.
* As a belt-and-braces against post-resume drift, a tiny `sync` on a
  short timer: `hwclock -s` (read the hardware clock — which Windows
  keeps accurate) or `wsl.exe ... ntpdate`. Document the chosen mechanism
  in `~/.config/omnisight/staging-compose.env`'s comments so the next
  operator knows it's there.
* Symptom-spotting: if probes start failing with TLS errors *all at once*
  after a host reboot/resume, suspect the clock first — `date -u` on 5c
  vs the prod host.

### 2.5 Network isolation — a docker network is **not** isolation on a shared LAN

On 5a, "isolation" was the Compose project name + cgroup quota — staging
and prod were the same machine, same kernel; nothing stopped staging code
from `dial`ing the prod Postgres if someone fat-fingered the DSN (that's
exactly why `verify-env-contract.sh` exists). On 5c, staging and prod are
**different machines on the same LAN segment** — which is *better*, but
the docker bridge network on 5c gives you **zero** protection: anything in
a staging container can open a socket to the prod host's IP just as easily
as your laptop can.

Real isolation on 5c requires **host-level network controls**, in order of
strength:

1. **VLAN / subnet separation (best).** Put 5c on its own VLAN; the only
   allowed flow to the prod VLAN is the specific one §5 needs (prod host
   → 5c:443 for the milestone checker, and the snapshot pull — see §5.3).
   Everything else: deny.
2. **Host firewall on 5c (minimum).** Windows Defender Firewall (with
   `firewallEnabled=true` in `.wslconfig`, mirrored mode honours it for
   the WSL distro): default-deny outbound to the prod host's IP except
   the snapshot-pull port; default-deny inbound except `:443` from the
   prod host. Also a `nftables`/`iptables` ruleset *inside* the distro as
   defence-in-depth.
3. **`verify-env-contract.sh` stays mandatory** as `ExecStartPre` — it is
   the last line of defence regardless of network topology, and it
   migrates unchanged.

Do **not** treat "it's a separate box now" as "it's isolated now". Same
LAN = same blast radius until a firewall or VLAN says otherwise. This is
the AUDIT-19 architecture's hard rule ("Network isolation enforced by
firewall/VLAN, NOT docker network alone" — see
`project_staging_gate_architecture.md` §Q7 risk class 5).

---

## 3. Migration runbook (AC #3) — step by step

Time budget: ~2–3 h of hands-on work, then a soak. Designed for a Friday
evening start.

Notation: **[5a]** = run on the old host, **[5c]** = run on the new WSL
distro, **[win]** = run in PowerShell on the 5c Windows host, **[prod]** =
run on the prod host, **[net]** = a change on the LAN router/switch.

### Step 0 — Pre-flight (do this *days* before, not on the night)

* [ ] **[5c]** WSL2 Ubuntu-24.04 is installed, `systemd=true` is set
      (§2.2), Docker Engine is installed *inside the distro* (§2.3),
      `$USER ∈ docker` group, `loginctl enable-linger "$USER"` set.
* [ ] **[win]** `.wslconfig` has `networkingMode=mirrored` (§2.1);
      `wsl --shutdown` done; `hostname -I` inside the distro is a LAN
      address you can `ping` from the prod host.
* [ ] **[5c]** `systemd-timesyncd` running (§2.4); `date -u` matches the
      prod host within seconds.
* [ ] **[5c]** Run the portability audit (§1.1) — **0 hits**. If not,
      stop and fix the offending artifact (devops change) first.
* [ ] **[net]** Decide the LAN plan: VLAN or firewall (§2.5, §5). Have
      the rule changes staged.
* [ ] **[prod]** Confirm `release_milestone_checker` currently passes its
      R3 gates against 5a (so you have a known-good baseline to compare
      against post-migration — AC #5/§4).

### Step 1 — Quiesce 5a staging

* [ ] **[5a]** Stop the timers so nothing is mid-deploy during the copy:
  ```bash
  systemctl --user stop staging-sync.timer staging-pg-snapshot.timer \
    staging-gate-canary.timer staging-gate-smoke.timer
  ```
* [ ] **[5a]** Bring the stack down cleanly (keeps the named volumes):
  ```bash
  systemctl --user stop omnisight-staging-compose.service
  docker ps --filter name=staging   # expect empty
  ```
* [ ] **[5a]** *Leave the units installed and the host as-is* — you may
      need to roll back to it (§ Rollback). Do **not** `disable` or wipe
      5a yet.

### Step 2 — Get the repo onto 5c

* [ ] **[5c]** Clone into the **Linux** filesystem (§2.3 — never
      `/mnt/c`):
  ```bash
  cd ~ && git clone <gitlab-remote> sora-bridge && cd sora-bridge
  git checkout main      # staging tracks main for the OP-878 deploy path;
                         # sync_staging_to_develop.sh fetches develop itself
  ```
* [ ] **[5c]** Record the absolute path you cloned into — call it
      `$REPO` (e.g. `/home/<user>/sora-bridge`). You will substitute it
      into the systemd units in step 5. Also pick `$LOGDIR`
      (e.g. `/home/<user>/work/sora/logs`) and `mkdir -p
      "$LOGDIR"/{release-milestone,staging}`.

### Step 3 — Recreate the host-side secrets/config

* [ ] **[5c]** Env file from the committed template, then fill in real
      values:
  ```bash
  cp infra/staging/.env.template deploy/staging/.env
  ${EDITOR:-nano} deploy/staging/.env       # set POSTGRES_PASSWORD, sandbox keys, etc.
  chmod 600 deploy/staging/.env
  ```
  If 5a had host-specific overrides in `deploy/staging/.env.local`, copy
  *that file* over (`scp 5a:.../deploy/staging/.env.local
  deploy/staging/`) — it is the only place a hard `host:port` is allowed
  to live (per the §1 audit).
* [ ] **[5c]** `mkdir -p ~/.config/omnisight && chmod 700 ~/.config/omnisight`,
      then re-create (or `scp` over, then `chmod 600`):
  * `~/.config/omnisight/release-audit.env` — `OMNISIGHT_DATABASE_URL=...`
    (the release-audit DSN the sync/snapshot/gate units write their audit
    row through).
  * `~/.config/omnisight/staging-compose.env` — any per-host overrides
    (`OMNISIGHT_STAGING_HEALTHZ_URL`, sandbox-key prefixes, …).
  * `~/.config/omnisight/staging-pg-snapshot.env`,
    `~/.config/omnisight/staging-sync.env` — per-host snapshot/sync knobs
    + `OMNISIGHT_STAGING_ALERT_WEBHOOK` (the *staging* Slack webhook).
  * `~/.config/omnisight/gerrit-claude-bot-ed25519` — **only** if you run
    the `bridge` profile; `chmod 600`.
* [ ] **[5c]** (Optional warm-start) restore the `staging-postgres`
      volume tarball if you took one (§0). Otherwise skip — the snapshot
      timer will refill it.

### Step 4 — DNS + firewall (the bit other machines depend on)

See §5 for the detail. The short version:

* [ ] **[net/DNS]** Repoint `staging.sora.services` → 5c's LAN IP (the
      mirrored-mode address from step 0). If you use a public name with a
      split-horizon resolver, update the *internal* view; the public view
      can stay (or point at a deny). TTL: drop it to 60 s a day before,
      bump back after.
* [ ] **[net]** VLAN or firewall (§2.5): allow `prod-host →
      5c:443` (milestone checker / gate probes) and the snapshot-pull
      path (§5.3); default-deny everything else between the staging and
      prod segments.
* [ ] **[prod]** `getent hosts staging.sora.services` → 5c IP;
      `curl -fsS https://staging.sora.services/healthz` — will 502/503
      until step 6 brings the stack up, but DNS + the firewall path
      should resolve/connect *now*.

### Step 5 — Install + path-fix the systemd units on 5c

The units ship with `/home/user/sora-bridge` / `/home/user/work/sora/logs`
prefixes (the 5a layout). Rewrite them to `$REPO` / `$LOGDIR` from step 2
as you install. **This is the only artifact editing the migration needs**
(by design — see §1.3):

```bash
mkdir -p ~/.config/systemd/user
REPO=/home/$USER/sora-bridge          # whatever you cloned into
LOGDIR=/home/$USER/work/sora/logs

for u in omnisight-staging-compose.service \
         staging-sync.service staging-sync.timer \
         staging-pg-snapshot.service staging-pg-snapshot.timer \
         staging-gate-canary.service staging-gate-canary.timer \
         staging-gate-smoke.service staging-gate-smoke.timer \
         staging-gate-alert.service; do
  sed -e "s#/home/user/sora-bridge#$REPO#g" \
      -e "s#/home/user/work/sora/logs#$LOGDIR#g" \
      -e "s#/home/user/.config#/home/$USER/.config#g" \
      "$REPO/deploy/systemd/$u" > "$HOME/.config/systemd/user/$u"
done
systemctl --user daemon-reload
```

> Sanity-check the rewrite: `grep -rn '/home/user/' ~/.config/systemd/user/`
> should be **empty**. (If `$USER` *is* `user`, the rewrite is a no-op and
> that's fine.)

### Step 6 — Bring the 5c stack up

* [ ] **[5c]** Contract check first (it's the unit's `ExecStartPre`, but
      dry-run it so a bad `.env` fails *before* systemd):
  ```bash
  OMNISIGHT_STAGING_ENV_FILE="$REPO/deploy/staging/.env" \
    infra/staging/verify-env-contract.sh
  # expect: "env contract OK — staging may start"
  ```
* [ ] **[5c]** Start the stack:
  ```bash
  systemctl --user enable --now omnisight-staging-compose.service
  systemctl --user status omnisight-staging-compose   # active (exited), no red
  docker ps --filter name=staging                     # postgres, backend-a/b, caddy, frontend
  curl -fsS http://localhost:9000/healthz             # 200
  ```
  First boot pulls the GHCR images — allow up to the unit's
  `TimeoutStartSec=300`.
* [ ] **[5c]** Re-enable the timers:
  ```bash
  systemctl --user enable --now staging-sync.timer staging-pg-snapshot.timer \
    staging-gate-canary.timer staging-gate-smoke.timer
  systemctl --user list-timers 'staging-*'
  ```
* [ ] **[5c]** Kick one cycle of each so you don't wait for the interval:
  ```bash
  systemctl --user start staging-sync.service          # deploy develop tip
  journalctl --user -u staging-sync -n 50
  systemctl --user start staging-pg-snapshot.service   # load an anonymized snapshot
  journalctl --user -u staging-pg-snapshot -n 50
  systemctl --user start staging-gate-canary.service staging-gate-smoke.service
  tail -n5 "$LOGDIR"/release-milestone/*-status.jsonl
  ```

### Step 7 — Verify (AC #4 + AC #5)

Run the verification test plan in §4. The bar: **same outcomes as 5a**.
Then from the prod side:

* [ ] **[prod]** `curl -fsS https://staging.sora.services/healthz` → 200
      (proves DNS + firewall + the stack — AC #5).
* [ ] **[prod]** `systemctl --user start release-milestone-checker.service`
      (or wait for its timer); `tail -n5
      .../logs/release-milestone/systemd.log` — the R3 gate verdict
      should be **the same** as it was against 5a (a fresh, non-override
      `run_id` reading the JSONL the 5c gate timers now write).

### Step 8 — Soak (Saturday), then decide (Sunday)

* [ ] Leave it running for ~24 h. Watch `journalctl --user -u
      'staging-*'` for any `OnFailure` → `staging-gate-alert` fires; watch
      the staging Slack channel; watch that `list-timers` keeps ticking
      (the WSL `enable-linger` check — §2.2).
* [ ] **Sunday — keep:** decommission 5a staging (next step). **Sunday —
      roll back:** § Rollback. Decide on *evidence* (timers ticking,
      gates green, no TLS-clock errors, milestone checker agrees), not
      vibes.

### Step 9 — Decommission 5a staging (only after a clean soak)

* [ ] **[5a]** `systemctl --user disable --now omnisight-staging-compose.service
      staging-sync.timer staging-pg-snapshot.timer staging-gate-canary.timer
      staging-gate-smoke.timer`
* [ ] **[5a]** `docker compose -f deploy/staging/docker-compose.yml down -v`
      (drops the now-stale staging volumes — the data lives on 5c now).
* [ ] **[5a]** `rm ~/.config/systemd/user/{omnisight-staging-compose,staging-*}.{service,timer}`;
      `systemctl --user daemon-reload`. Leave `~/.config/omnisight/*.env`
      if prod uses any of them; otherwise remove.
* [ ] Update [`staging-environment-runbook.md`](staging-environment-runbook.md)
      to point at 5c, and close the OP-927 / AUDIT-19 META.

---

## 4. Verification test plan (AC #4)

The acceptance bar for the migration is: **re-run the AUDIT-19a/b/c test
suites and the bring-up checklists on 5c, get the same outcomes as 5a.**

### 4.1 Static suites (run on 5c, in the repo clone)

| Suite | Command | Pass == |
|---|---|---|
| AUDIT-19a — compose unit + env contract | `pytest -q tests/test_staging_compose_unit.py` | all green (it `exec`s `verify-env-contract.sh`, so it also proves the guard works on this host) |
| AUDIT-19b — snapshot/anonymize pipeline | `pytest -q backend/tests/test_staging_snapshot_restore.py` | all green |
| AUDIT-19c — develop→staging sync | `pytest -q backend/tests/test_staging_sync.py` | all green |
| AUDIT-19d — portability audit + runbook coverage | `pytest -q tests/test_staging_migration_5a_to_5c.py` | all green, **0 portability hits** |
| (sanity) staging gate producers | `pytest -q backend/tests/test_staging_gate.py` | all green |

These are pure-Python/bash, no docker/systemd needed — they should be
green on any clone, so this step really validates "the clone is intact and
the host has Python+bash", which is necessary but not sufficient.

### 4.2 Live bring-up checklist (run on 5c, against the running stack)

Re-run the AC checklist from
[`staging-environment-runbook.md` §3](staging-environment-runbook.md) **on
5c**:

| AC | Command | Expect (same as 5a) |
|---|---|---|
| AC1 — stack up cleanly | `systemctl --user is-active omnisight-staging-compose.service` | `active` |
| AC2 — containers running | `docker ps --filter name=staging --format '{{.Names}}'` | `postgres`, `backend-a`, `backend-b`, `caddy`, `frontend` (+ `bridge-daemon` if profile on) |
| AC3 — healthz 200 | `curl -fsS http://localhost:9000/healthz` | `200` |
| AC5 — cgroup quota | `systemctl --user show omnisight-staging-compose.service -p MemoryMax -p CPUQuotaPerSecUSec -p IOWeight` | `MemoryMax` ≈ 30% of *5c's* RAM (auto-scaled — that's the point), `CPUQuotaPerSecUSec=300ms`, `IOWeight=50` |
| AC (19c) — sync ran | `journalctl --user -u staging-sync -n 20` | a `staging on develop tip <sha>` line, exit 0 |
| AC (19b) — snapshot ran | `journalctl --user -u staging-pg-snapshot -n 20` ; `PGPASSWORD=… psql -h 127.0.0.1 -p 55432 -U omnisight -d postgres -c '\l'` | exit 0; `omnisight_staging` (+ `_prev` after the 2nd run) present |
| AC (gates) — JSONL fresh | `tail -n3 "$LOGDIR"/release-milestone/canary-status.jsonl "$LOGDIR"/release-milestone/smoke-status.jsonl` | recent timestamps, `revision:<develop tip>`, status the same class as on 5a |

### 4.3 Cross-host check (the AC #5 one) — see §5.4.

If any 5c outcome differs from the 5a baseline you recorded in step 0,
**that is a migration defect** — don't proceed to step 9 (decommission);
diagnose (the §2 gotchas are the usual suspects: clock for TLS errors,
mirrored-mode/linger for "timers don't run", `/mnt/c` for "everything is
slow"), or roll back.

---

## 5. DNS / firewall — staging on 5c reachable from prod's `release_milestone_checker` (AC #5)

### 5.1 Who needs to reach staging, and why

`scripts/release_milestone_checker.py` runs on the **prod** host and gates
RELEASE R3. It does *not* hit staging directly — it reads
`canary-status.jsonl` / `smoke-status.jsonl`. But those files are written
by `deploy/systemd/staging-gate-{canary,smoke}.service`, which run
`backend/agents/staging_gate.py`, which **HTTP-probes
`$OMNISIGHT_STAGING_URL`** (default `https://staging.sora.services`,
`/healthz` + `/readyz`, and the smoke suite against the same base URL).

Where do those gate units run? Today, on the **same host** as the staging
stack (5a → 5c). So:

* If the gate units run **on 5c** (recommended — they ship in the same
  `deploy/systemd/` set this runbook installs in step 5): the probe is
  `5c → localhost`, trivially reachable. The cross-host requirement then
  reduces to "the **prod** host can read the JSONL files" — which, today,
  means the JSONL files must be on a path the prod-host checker reads. If
  the checker reads them over a share/sync from 5c, that path must exist;
  if (simpler) the gate units write to a directory the prod checker also
  mounts, document that mount. **Decide and document which host writes the
  R3 JSONL** as part of step 5 — it is the one piece of staging-gate
  topology this migration must not leave ambiguous.
* If, instead, you run the gate units **on the prod host** pointing
  `OMNISIGHT_STAGING_URL=https://staging.sora.services` at 5c: then the
  prod host must reach `5c:443` over the LAN — that is the firewall rule
  in §5.3.

Either way, the **`release_milestone_checker` ↔ staging-on-5c** path must
be proven (§5.4). The safest topology: gate units on 5c, JSONL written to
a path the prod checker reads; prod → 5c:443 still allowed (it's cheap and
lets an operator `curl` staging from prod for debugging).

### 5.2 DNS

* `staging.sora.services` must resolve, **from the prod host and from 5c
  itself**, to 5c's LAN IP (the `networkingMode=mirrored` address — stable
  as long as 5c's DHCP lease/reservation is stable; pin a DHCP reservation
  for 5c's MAC).
* If `staging.sora.services` is a public name: use the split-horizon
  resolver (the LAN's internal view → 5c LAN IP). Keep the public view
  pointing nowhere useful (or at a deny) — staging must not be
  Internet-reachable.
* TTL hygiene: drop the record's TTL to 60 s the day before the cut-over;
  restore it after the soak.
* Caddy: the staging Caddy serves `${STAGING_HOSTNAME:-staging.sora.services}`
  and gets its cert via ACME — which needs the name to resolve *and* a
  reachable ACME challenge path. On a LAN-only name, use an internal ACME
  (e.g. step-ca) or a long-lived internal cert mounted into Caddy; don't
  rely on public Let's Encrypt for a name that isn't publicly reachable.
  (This is unchanged from 5a — just re-confirm it on 5c.)

### 5.3 Firewall / VLAN rules (the minimum allow-list)

On the LAN (router ACL if VLAN-separated; Windows Defender Firewall on 5c
+ host firewall on prod otherwise):

| Direction | Port | Why | Default for everything else |
|---|---|---|---|
| prod-host → 5c | TCP 443 | milestone-checker / operator `curl` of staging `/healthz`; gate probes if you run them on prod | **deny** |
| 5c → prod-host | TCP 5432 (the prod PG primary's port, or whatever `snapshot-restore.sh` reaches it on) **OR** `docker exec` locality | `snapshot-restore.sh` `pg_dump`s prod via the `omnisight-pg-primary` container — if that container is reachable only on the prod host, 5c needs a path to it (SSH-tunnelled `docker exec`, or a firewalled PG port). **Pick one and document it.** | **deny** |
| 5c → Internet | TCP 443 | GHCR image pulls (`docker pull ghcr.io/...`), GitLab `git fetch` in `sync_staging_to_develop.sh`, Anthropic/Stripe **sandbox** endpoints, the staging Slack webhook | restrict to those hosts if you can; otherwise allow 443 out, deny in |
| 5c ↔ prod, anything else | — | — | **deny** (this is the §2.5 hard rule — same LAN ≠ isolated) |

> The `5c → prod` snapshot-pull rule is the one that needs a real
> decision: today `snapshot-restore.sh` runs `pg_dump` *through the prod
> Postgres container* (`docker exec omnisight-pg-primary pg_dump ...`).
> When the snapshot orchestrator runs on a *different* box (5c), it needs
> either (a) an SSH connection to the prod host to `docker exec` there, or
> (b) the prod PG port exposed to 5c's IP only. Option (a) keeps the PG
> port closed and is preferred; it means `snapshot-restore.sh` needs an
> `ssh prod-host docker exec ...` wrapper — an `area:devops` follow-up on
> the snapshot script, *not* a blocker for the host move (the rest of the
> pipeline migrates fine; the snapshot timer just errors `prod
> unreachable`/exit 5 until the wrapper lands, which is a clean,
> alerting, non-corrupting failure mode).

### 5.4 Prove it (the AC #5 verification)

From the **prod host**:

```bash
getent hosts staging.sora.services            # -> 5c LAN IP
curl -fsS https://staging.sora.services/healthz   # -> 200  (DNS + firewall + stack)
curl -fsS https://staging.sora.services/readyz    # -> 200
```

Then exercise the actual consumer:

```bash
# on whichever host runs the gate units (5c if you followed §5.1):
systemctl --user start staging-gate-canary.service staging-gate-smoke.service
# on the prod host:
systemctl --user start release-milestone-checker.service     # or wait for the timer
tail -n5 .../logs/release-milestone/systemd.log
# expect: an R3 verdict (ready/blocked) from a fresh run_id that is NOT a
# manual-override-* id, reading JSONL with revision:<current develop tip>,
# and the SAME verdict class it gave against 5a.
```

If `curl` from prod fails: DNS (does the name resolve to 5c?) → firewall
(can prod reach 5c:443?) → stack (is `omnisight-staging-compose.service`
active on 5c?), in that order. If `curl` works but the milestone checker
still sees stale JSONL: the §5.1 "which host writes the JSONL" question
wasn't answered — the gate units are writing somewhere the prod checker
isn't reading.

---

## 6. Rollback

You haven't touched 5a until step 9, so rollback is fast:

1. **[net/DNS]** Repoint `staging.sora.services` back to 5a's IP (60 s TTL
   from step 0 means this propagates fast). Revert the firewall/VLAN
   changes.
2. **[5a]** `systemctl --user start omnisight-staging-compose.service` then
   `systemctl --user start staging-sync.timer staging-pg-snapshot.timer
   staging-gate-canary.timer staging-gate-smoke.timer`. The 5a volumes are
   still there; the stack comes back to where step 1 left it.
3. **[5c]** `systemctl --user disable --now omnisight-staging-compose.service
   staging-*.timer` — leave the clone/config in place for the next attempt.
4. **[prod]** `curl -fsS https://staging.sora.services/healthz` → 200
   (now hitting 5a again); milestone checker recovers on its next tick.
5. Write up what bit you (almost always one of the §2 gotchas) before the
   next attempt.

The only thing rollback *loses* is any develop tip / snapshot that landed
on 5c during the soak — staging data is reconstructible, so that's
nothing.

---

## 7. Quick reference — the audit + verification commands

```bash
# Portability audit (DoD gate — must be 0 hits):
pytest -q tests/test_staging_migration_5a_to_5c.py -k portability

# Full AUDIT-19 regression on 5c:
pytest -q tests/test_staging_compose_unit.py \
          backend/tests/test_staging_snapshot_restore.py \
          backend/tests/test_staging_sync.py \
          backend/tests/test_staging_gate.py \
          tests/test_staging_migration_5a_to_5c.py

# Live bring-up (on 5c):
systemctl --user is-active omnisight-staging-compose.service
docker ps --filter name=staging
curl -fsS http://localhost:9000/healthz
systemctl --user list-timers 'staging-*'

# Cross-host (on prod):
curl -fsS https://staging.sora.services/healthz
systemctl --user start release-milestone-checker.service && \
  tail -n5 .../logs/release-milestone/systemd.log
```

---

## Change log

| Date | Ticket | Change |
|---|---|---|
| 2026-05-12 | OP-974 | Initial 5a → 5c migration runbook + portability audit (`tests/test_staging_migration_5a_to_5c.py`). |
