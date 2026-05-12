# Staging Environment Runbook — AUDIT-19a (OP-971)

**Owner:** Deploy on-call · **Spec:** META AUDIT-19 · **Ticket:** OP-971

This runbook is the boot / teardown / troubleshooting procedure for the
**systemd-wrapped** staging stack on the 5a host. It does not replace the
deploy-flow runbooks for staging — those still apply:

* [`staging-environment.md`](staging-environment.md) — OP-767 stack
  topology + the `main_promoted` auto-deploy worker.
* [`staging-runbook.md`](staging-runbook.md) — OP-878 blue-green
  `scripts/staging_deploy.sh` flow + Caddy ingress switch.

What OP-971 adds on top of those: a **systemd user unit**
(`omnisight-staging-compose.service`) that owns the stack's lifecycle on
the box, an **env-contract guard** (`infra/staging/verify-env-contract.sh`)
that runs before the stack starts, and a **cgroup quota** so staging can
never starve prod on the co-tenanted host.

> **Why co-tenanted?** Until the OP-927 dedicated develop-tracking
> staging host exists, staging runs as a second Docker Compose project on
> the same machine as prod. The name-suffixed services
> (`omnisight-staging-*`), the dedicated compose network, the
> staging-only ports, and the cgroup ceiling are what keep the two
> stacks from colliding.

---

## 0. Components at a glance

| Artifact | Path | Role |
| --- | --- | --- |
| Compose file | `deploy/staging/docker-compose.yml` | services (postgres, backend-a/b, caddy, frontend, bridge) — OP-767/878, unchanged |
| Caddy config | `deploy/staging/caddy.json` | in-stack reverse proxy — OP-767, unchanged |
| systemd unit | `deploy/systemd/omnisight-staging-compose.service` | brings the compose up under `systemctl --user`, applies cgroup quota |
| Env contract | `infra/staging/verify-env-contract.sh` | `ExecStartPre` guard — refuses to start if env points at prod |
| Env template | `infra/staging/.env.template` | copy to `deploy/staging/.env` and fill in |
| Blue-green | `scripts/staging_deploy.sh` | OP-878 auto-deploy on `change-merged` (orthogonal to this unit) |

The systemd unit runs **user-level** (`systemctl --user`), matching the
`staging-gate-*` and `gerrit-jira-bridge` units — it must share the
operator's docker access and home-dir paths, and it must not need sudo.

---

## 1. First-time install

```bash
# 1. Create the staging env file from the template and fill it in.
cp infra/staging/.env.template /home/user/sora-bridge/deploy/staging/.env
${EDITOR:-nano} /home/user/sora-bridge/deploy/staging/.env
chmod 600 /home/user/sora-bridge/deploy/staging/.env       # it holds secrets

#    Dry-run the contract check before wiring systemd:
OMNISIGHT_STAGING_ENV_FILE=/home/user/sora-bridge/deploy/staging/.env \
  environment=staging infra/staging/verify-env-contract.sh
#    Expect: "env contract OK — staging may start"

# 2. Install the user unit.
mkdir -p ~/.config/systemd/user
cp deploy/systemd/omnisight-staging-compose.service ~/.config/systemd/user/
systemctl --user daemon-reload

# 3. Enable + start. `enable --now` also brings it up on the next login;
#    `loginctl enable-linger` makes it boot without an interactive login.
systemctl --user enable --now omnisight-staging-compose.service
loginctl enable-linger "$USER"
```

Optional per-host overrides go in `~/.config/omnisight/staging-compose.env`
(read via `EnvironmentFile=-…`, so it is optional):

```bash
# example overrides
OMNISIGHT_STAGING_HEALTHZ_URL=http://localhost:9000/healthz
OMNISIGHT_STAGING_REQUIRE_CGROUP_V2=1     # make CgroupV1Fallback fatal
```

---

## 2. Daily operations

| Action | Command |
| --- | --- |
| Start staging | `systemctl --user start omnisight-staging-compose` |
| Stop staging | `systemctl --user stop omnisight-staging-compose` |
| Restart | `systemctl --user restart omnisight-staging-compose` |
| Status + last logs | `systemctl --user status omnisight-staging-compose` |
| Follow logs | `journalctl --user -u omnisight-staging-compose -f` |
| List staging containers | `docker ps --filter name=staging` |
| Stack health (in-container) | `docker compose -f deploy/staging/docker-compose.yml ps` |
| Healthz from host | `curl -fsS http://localhost:9000/healthz` |

`ExecStart` is `docker compose … up -d --wait --remove-orphans`, so
`systemctl --user start` **blocks** until every service's healthcheck is
green, and exits non-zero (the unit goes `failed`) if the stack does not
converge inside `TimeoutStartSec=300`. `ExecStartPost` then curls
`/healthz`; a non-200 there also fails the unit and triggers `ExecStop`
(`docker compose … down`). So a green `systemctl --user status` means:
contract passed → all services healthy → `/healthz` returned 200.

`ExecStop` is `docker compose … down` — a clean `systemctl --user stop`
removes the staging containers and the staging compose network but keeps
the named volumes (`staging-postgres`, `staging-artifacts`, …).

---

## 3. Bring-up verification (AC checklist)

Run after `systemctl --user start`:

```bash
# AC1 — services up cleanly
systemctl --user is-active omnisight-staging-compose         # => active

# AC2 — staging containers running alongside prod, isolated by suffix + net
docker ps --format '{{.Names}}\t{{.Status}}' | grep staging
docker network ls | grep staging
docker ps --format '{{.Names}}' | grep -E 'pg-primary|omnisight-.*-1'   # prod still there

# AC3 — staging healthz returns 200
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:9000/healthz   # => 200

# AC5 — cgroup quota applied
systemd-cgls /user.slice/user-1000.slice/user@1000.service/app.slice/omnisight-staging-compose.service
systemctl --user show omnisight-staging-compose \
  -p MemoryMax -p CPUQuotaPerSecUSec -p IOWeight
#   MemoryMax => ~30% of total RAM in bytes; CPUQuotaPerSecUSec => 300ms; IOWeight=50

# AC6 — prod untouched
docker inspect -f '{{.State.Health.Status}}' pg-primary       # => healthy
docker compose -f docker-compose.prod.yml ps                  # all Up/healthy
```

---

## 4. Env contract (`verify-env-contract.sh`)

Runs as `ExecStartPre`. It loads `deploy/staging/.env` (and a sibling
`.env.local` if present — inherited env wins over both), then enforces:

1. **Postgres DSN must look like staging.** `OMNISIGHT_DATABASE_URL` /
   `DATABASE_URL` (or the reconstructed `POSTGRES_DB` + `STAGING_POSTGRES_PORT`)
   must have a database name ending `-staging` / `_staging`, **or** connect
   on a staging Postgres port (`55432` / `55433`). Otherwise →
   `EnvContractViolation`, exit 1.
2. **External API keys must carry sandbox prefixes.**
   `STRIPE_SECRET_KEY` must start `sk_test_` (an `sk_live_` value is an
   instant fail); `ANTHROPIC_API_KEY` / `OMNISIGHT_ANTHROPIC_API_KEY` must
   be empty or a key whose name contains `test`/`sandbox`/`eval`;
   `SLACK_WEBHOOK_URL` / `OMNISIGHT_STAGING_ALERT_WEBHOOK` /
   `SLACK_ALERT_WEBHOOK` must be empty or contain `staging`. Otherwise →
   `EnvContractViolation`, exit 1.
3. **`environment=staging` must be exported** (the unit also exports
   `OMNISIGHT_ENV=staging` as the canonical alias; either satisfies the
   check). Otherwise → `EnvContractViolation`, exit 1.

Two non-blocking checks (warn only, unless opted into):

* **`PortCollisionWithProd`** (warn by default; exit 2 only if
  `OMNISIGHT_STAGING_STRICT_PORTS=1`) — a configured staging port (`9000`
  healthz, `8080` http, `8010`/`8011` backends, `55432` postgres, plus
  `OMNISIGHT_STAGING_EXTRA_PORTS`) is already in use. Fix: change the
  colliding `STAGING_*_PORT` in `deploy/staging/.env` or `.env.local`
  (`up -d --wait` would fail with `bind: address already in use` anyway).
* **`CgroupV1Fallback`** (warn; exit 3 only if
  `OMNISIGHT_STAGING_REQUIRE_CGROUP_V2=1`) — the kernel exposes only
  cgroup v1, so the unit's `MemoryMax=30%` / `CPUQuota=30%` percentage
  limits silently no-op. See §6.

Run it by hand any time:

```bash
OMNISIGHT_STAGING_ENV_FILE=/home/user/sora-bridge/deploy/staging/.env \
  environment=staging infra/staging/verify-env-contract.sh; echo "exit=$?"
```

---

## 5. Troubleshooting

### `EnvContractViolation` — staging pointed at prod

Symptom: `systemctl --user start` fails immediately; `journalctl --user -u
omnisight-staging-compose` shows
`[verify-env-contract.sh] EnvContractViolation: …` and the containers were
never created.

Cause: `deploy/staging/.env` (or `.env.local`) carries a prod Postgres
DSN, an `sk_live_` Stripe key, a prod Anthropic key, a non-staging Slack
webhook, or is missing `environment=staging`.

Fix:

```bash
${EDITOR:-nano} /home/user/sora-bridge/deploy/staging/.env       # correct the offending value
OMNISIGHT_STAGING_ENV_FILE=/home/user/sora-bridge/deploy/staging/.env \
  environment=staging infra/staging/verify-env-contract.sh        # re-verify -> "env contract OK"
systemctl --user start omnisight-staging-compose
```

This is the intended behaviour, not a bug — the guard exists precisely so
a fat-fingered `.env.local` on the co-tenanted box can't bring staging up
against prod Postgres.

### `PortCollisionWithProd` — staging port in use

Symptom: contract check warns `PortCollisionWithProd: port <N> is already
bound`, and/or `up -d --wait` later fails with `bind: address already in
use`.

Fix: pick a free port and set it in `deploy/staging/.env` /
`.env.local` — e.g. `STAGING_HTTP_PORT=8081`,
`OMNISIGHT_STAGING_HEALTHZ_PORT=9001` (and mirror the latter into
`~/.config/omnisight/staging-compose.env` as
`OMNISIGHT_STAGING_HEALTHZ_URL` so the unit's `ExecStartPost` curls the
right port). Then `systemctl --user restart omnisight-staging-compose`.

### `CgroupV1Fallback` — kernel has no cgroup v2

Symptom: contract check warns `CgroupV1Fallback: cgroup v2 not detected`;
`systemctl --user show … -p MemoryMax` shows `infinity`; `journalctl
--user` around start shows `Failed to set MemoryMax` / `Unknown control
group property`.

Options:

1. **Switch the host to the cgroup v2 unified hierarchy** (preferred).
   On WSL: set `cgroupv2=1` (or `systemd.unified_cgroup_hierarchy=1`) for
   the WSL kernel and restart. Then the percentage limits work as-is.
2. **Pin absolute byte limits in a drop-in** instead of percentages —
   v1 honours absolute `MemoryMax=…`/`CPUQuota=…` even though the user
   manager's percentage math needs v2:

   ```bash
   mkdir -p ~/.config/systemd/user/omnisight-staging-compose.service.d
   cat > ~/.config/systemd/user/omnisight-staging-compose.service.d/cgroup-v1.conf <<'EOF'
   [Service]
   MemoryMax=8G
   CPUQuota=200%
   IOWeight=
   EOF
   systemctl --user daemon-reload
   systemctl --user restart omnisight-staging-compose
   ```

   Size the absolute ceiling at ≈30% of the box's RAM / ≈2 cores.
3. **Refuse to start** until the host is fixed — set
   `OMNISIGHT_STAGING_REQUIRE_CGROUP_V2=1` in
   `~/.config/omnisight/staging-compose.env`; the contract check then
   exits 3 and the unit stays down rather than running uncapped next to
   prod.

### Stack won't converge inside `TimeoutStartSec=300`

`journalctl --user -u omnisight-staging-compose` shows a service stuck
`starting`/`unhealthy`. Inspect it directly:

```bash
docker compose -f deploy/staging/docker-compose.yml ps
docker compose -f deploy/staging/docker-compose.yml logs --tail=200 <service>
```

Common causes: GHCR pull throttling (`OMNISIGHT_IMAGE_TAG` not yet
published — wait for the D2 image), Postgres failing on a bad
`POSTGRES_PASSWORD`, or `/healthz` not wired to port `9000` on this host
(check `OMNISIGHT_STAGING_HEALTHZ_PORT` vs the Caddy/backends config).

### `/healthz` 200 but `systemctl status` still shows `activating`

`up -d --wait` is still polling another service's healthcheck. Wait for
it, or `journalctl --user -u omnisight-staging-compose -f` to see which
service is the laggard.

---

## 6. Teardown

```bash
# Graceful: removes staging containers + network, keeps volumes.
systemctl --user stop omnisight-staging-compose

# Don't auto-start on next login either:
systemctl --user disable omnisight-staging-compose

# Full wipe (also drops the staging Postgres / artifact volumes):
docker compose -f deploy/staging/docker-compose.yml down -v
```

Removing the unit entirely:

```bash
systemctl --user disable --now omnisight-staging-compose
rm ~/.config/systemd/user/omnisight-staging-compose.service
systemctl --user daemon-reload
```

---

## 7. Relationship to the blue-green auto-deploy (OP-878)

`scripts/staging_deploy.sh` runs **separate** colour-suffixed compose
projects (`omnisight-staging-blue` / `-green`) and switches the *host*
Caddy ingress. This systemd unit instead owns the **default** compose
project from `deploy/staging/docker-compose.yml` (no colour suffix) — it
is the "always-on baseline staging" lifecycle, the thing AUDIT-19b/19c
build their gates against. The two don't fight: the blue-green flow uses
`-p omnisight-staging-<colour>` project names, this unit uses the compose
file's default project name. If you adopt the blue-green flow as the
primary on this host, disable this unit so there's a single owner of the
default project.

---

## Change log

| Date | Ticket | Change |
| --- | --- | --- |
| 2026-05-12 | OP-971 | Initial runbook — systemd wrapper, env contract, cgroup quota, bring-up verification. |
