# OmniSight Manual Failover Runbook (G6 #3 / HA-06)

> G6 #3 (TODO row 1381). Third deliverable of the G6 HA-06 bucket —
> the operator-facing **step-by-step** for the two failure modes the
> RTO budget in [`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) is sized
> against:
>
> 1. **Database primary host dies** — promote the standby and re-point
>    the application within the 15-min RTO.
> 2. **Reverse proxy (Caddy) fails** — fall back to a direct-to-backend
>    or replacement-edge path while keeping the RTO budget intact.
>
> The companion runbooks this one rides on top of:
>
> * [`docs/ops/db_failover.md`](db_failover.md) — G4 #6: the *full*
>   PostgreSQL HA + cutover playbook (planned + unplanned promote +
>   rebuild + forensics). This file extracts the **manual primary-down
>   switch** path into a ≤ 15-min decision tree and adds the
>   reverse-proxy failure mode it does not cover.
> * [`docs/ops/blue_green_runbook.md`](blue_green_runbook.md) — G3:
>   the app-layer cutover the DB failover rides on top of.
> * [`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) — G6 #2: the RTO ≤ 15 min
>   / RPO ≤ 5 min budget this runbook is bounded by. Every numbered
>   step below has a budget allocation that sums to the §2.2 split
>   (detect 0–2 / diagnose 2–5 / execute 5–12 / verify 12–15).
>
> This runbook is what an oncall reads at 3am when:
>
> * `docker compose ps` shows `pg-primary` dead and `pg-standby`
>   healthy (§2).
> * `curl -sf https://<host>/readyz` returns no answer / connection
>   refused even though both backend replicas are healthy (§3).
> * The Caddy container itself is crash-looping or wedged (§3).
> * `pg-primary` is back but the standby was promoted in the meantime
>   and the topology is "two would-be primaries" (§4).
>
> What this runbook does NOT cover (explicit scope fence — owned by
> sibling G6 / G7 rows, see §6):
>
> * The **automated** daily DR drill — owned by G6 #1
>   (`.github/workflows/dr-drill-daily.yml`).
> * The annual operator-led DR drill checklist — owned by G6 #4
>   (row 1382).
> * The `scripts/dr_drill.sh` operator-runnable wrapper + the bundle-
>   closure `docs/ops/dr_runbook.md` aggregator — owned by G6 #5
>   (row 1383).
> * Prometheus / Grafana dashboards + alert rules that *fire* the page
>   that triggers this runbook — owned by G7 (rows 1385–1388).

---

## 1. Decision tree (read this first)

```
Page fired — what is broken?
│
├── App returns 5xx / `/readyz` 503 from BOTH backend replicas
│   AND `psql` to the primary refuses connections
│   → §2  Database primary failover
│
├── App returns 5xx / connection refused at the EDGE (https://<host>)
│   BUT both backend replicas answer `/readyz` 200 on their host ports
│   → §3  Reverse proxy fallback
│
├── BOTH of the above
│   → §2 first (DB is upstream of the proxy — fixing the proxy first
│        wastes the 15-min RTO budget on the wrong layer), then §3
│
└── Neither — single backend replica down, transient 5xx, etc.
    → out of scope for this runbook; see G2 rolling-restart runbook
      (`scripts/deploy.sh --strategy rolling`) or G3 blue-green
      (`docs/ops/blue_green_runbook.md`)
```

The two paths are independent — you can be in §2 without §3 and vice
versa. The decision tree above is the only thing you must read before
acting; the rest of the runbook is reference once you know which
section you are in.

---

## 2. Database primary failover (manual switch)

### 2.1 When to invoke this section

Either of the following is **sufficient** to invoke this section:

* `pg-primary` container is `Restarting (…)` or `Exit N` for **more
  than 5 minutes** AND a 1-command fix (`docker compose restart
  pg-primary`, free disk, etc.) is not available.
* `pg-primary` host is unreachable on the network (link down,
  hardware failure) and there is no bounded ETA for it to return.

If either is true and the standby is still streaming, the **decision
to promote** is the right call inside the RTO budget.

If neither is true (transient blip, primary is recovering on its own,
operator is still gathering signal) **do NOT promote** — a promote is
one-way: the old primary becomes a diverged timeline that has to be
rebuilt from the new primary (§4 / `db_failover.md` §7).

### 2.2 Step-by-step (target: ≤ 12 min from page to `/readyz` 200)

The numbered budget allocations below add up to the 15-min RTO from
[`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) §2.2. Anything outside the
budget is a runbook bug — file a G6 post-mortem, do not silently
extend the budget.

#### Step 1 — Detect + declare (0–2 min)

Confirm the failure on both layers before paging anyone else:

```bash
# Is the primary container alive?
docker compose -f deploy/postgres-ha/docker-compose.yml ps

# What is the standby seeing?
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    psql -U omnisight -d omnisight -c \
    "SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn(),
            now() - pg_last_xact_replay_timestamp() AS replay_lag;"
```

Acknowledge the page. Open `docs/ops/db_failover.md` in tab 2 and
this file in tab 1 — both are referenced below.

#### Step 2 — Diagnose + decide (2–5 min)

Read `docker compose logs --tail 200 pg-primary` for the failure
signature. Decide path A or B from the table:

| Symptom | Path | Reason |
|---|---|---|
| OOM-killed, host disk full, container OOM, transient kernel error | A: fix primary | If the primary is recoverable in < 5 min, fixing is cheaper than promote + rebuild |
| Primary unreachable on the network, host hardware failure, kernel panic, > 5 min elapsed without progress | B: promote standby | Bounded RTO must beat unbounded "wait for hardware" |
| Primary container alive but `pg_is_in_ready` 503 (DB process wedged, PGDATA corruption) | B: promote standby | Standby has a clean Merkle-chained tail; primary is suspect |

If **path A**, follow `docs/ops/db_failover.md` §6.1 ("Decide:
transient or persistent?") — single command, then re-test, return to
normal ops without exiting this section.

If **path B**, continue to step 3.

#### Step 3 — Execute: promote standby (5–8 min)

**Pre-promote check** (under 30 s — do NOT skip even if the
primary is unreachable):

```bash
# What is the standby's last replicated transaction timestamp?
# If this is older than RPO budget (5 min), the data-loss window
# exceeds the contract — proceed but flag in the post-mortem.
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    psql -U omnisight -d omnisight -c \
    "SELECT pg_last_xact_replay_timestamp(),
            now() - pg_last_xact_replay_timestamp() AS replay_lag;"
```

**Promote** (the load-bearing 30-second moment):

```bash
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    pg_ctl promote -D /var/lib/postgresql/data/pgdata
```

**Confirm promotion** (≤ 5 s):

```bash
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    psql -U omnisight -d omnisight -c "SELECT pg_is_in_recovery();"
# Expected: f  (false = standby is now accepting writes)
```

If `pg_ctl promote` exits non-zero, jump to `db_failover.md` §5.4 —
do not retry blindly.

#### Step 4 — Execute: re-point the application (8–12 min)

The application reads `OMNISIGHT_DATABASE_URL` at boot (see
`docs/ops/bootstrap_modes.md`). Flip it to the new primary's
host:port (the ex-standby is on `5433` by default, see
`PG_STANDBY_HOST_PORT` in `deploy/postgres-ha/.env.example`):

```bash
# In the env file the app reads (docker-compose.prod.yml or systemd
# unit env, depending on your deploy):
OMNISIGHT_DATABASE_URL="postgresql+asyncpg://omnisight:${POSTGRES_PASSWORD}@<new-primary-host>:5433/omnisight"
```

Restart **one** backend replica at a time so the live traffic is
not double-disrupted. Use the blue-green flow if a blue-green
ceremony is in progress, otherwise the rolling-restart flow:

```bash
# Rolling: restart backend-a, wait /readyz, then backend-b.
# (See scripts/deploy.sh --strategy rolling for the full flow.)
docker compose -f docker-compose.prod.yml up -d --no-deps \
    --force-recreate backend-a
curl -sf http://localhost:8000/readyz
docker compose -f docker-compose.prod.yml up -d --no-deps \
    --force-recreate backend-b
curl -sf http://localhost:8001/readyz
```

#### Step 5 — Verify + acknowledge (12–15 min)

`/readyz` 200 from both replicas is the canonical "service is back"
signal (per [`docs/ops/dr_rto_rpo.md`](dr_rto_rpo.md) §2.4):

```bash
# Edge-level (through the proxy):
curl -sf https://<host>/readyz

# Both backend replicas (bypassing the proxy):
curl -sf http://localhost:8000/readyz && echo blue-OK
curl -sf http://localhost:8001/readyz && echo green-OK
```

Post the "we're back" message in the incident channel, update the
ticket, and continue with §2.3 (post-action follow-ups).

### 2.3 After the promote (within the next 24 h, NOT in the RTO budget)

* **Rebuild the old primary as the new standby** —
  `docs/ops/db_failover.md` §7 is the canonical procedure
  (`pg_basebackup` via `init-standby.sh`). Until this is done the
  topology is "primary alone" and a second failure is not survivable.
* **Slot cleanup** — if the old primary had unique-named replication
  slots, drop them per `db_failover.md` §7.2.
* **Data-loss accounting** — under async replication, any write
  COMMITted on the old primary but not yet shipped to the standby is
  lost. Quantify per `db_failover.md` §6.3 — the audit_log Merkle
  chain on the (new) primary ends at the last replicated `curr_hash`,
  so the missing tail is **not forgeable** later. File a post-mortem
  if `replay_lag` at promote time exceeded the 5-min RPO.
* **Update the incident ticket** with the actual `t₀ → t₁` wall-clock
  time and which step blew (or stayed inside) its budget. This is
  what feeds the next G6 #4 annual checklist (row 1382).

### 2.4 Rollback ("we promoted but the new primary is worse")

A promote is one-way as far as PostgreSQL is concerned (the old
primary's WAL diverged the moment new writes landed on the standby).
Operationally, "rollback" means one of:

* **The old primary is recoverable AND has more recent committed
  WAL than the standby had at promote time** — accept the data-loss
  window, restore from the backup artefact (G6 #1 daily drill output)
  + replay WAL up to the loss point. This is forensic work, not part
  of the 15-min budget. Treat it as a separate incident.
* **The new primary is misbehaving but the old data is fine** —
  rebuild the new primary box from the backup
  (`scripts/backup_selftest.py` against the latest daily artefact),
  re-point the app, then proceed with §2.3 normally.

If you find yourself wanting to "un-promote" the standby, stop and
escalate — that's a path that has eaten more weekends than any
tooling could rescue.

---

## 3. Reverse proxy (Caddy) fallback

### 3.1 When to invoke this section

Both of the following are **necessary** to invoke this section:

* The edge endpoint (`https://<host>/readyz`) returns connection
  refused / 5xx / a TLS handshake error.
* Both backend replicas answer `/readyz` 200 on their host ports
  (`http://localhost:8000/readyz` and `http://localhost:8001/readyz`).

If either backend replica is also red, the proxy is doing the right
thing by ejecting it (per `deploy/reverse-proxy/Caddyfile` passive
ejection knobs) and the fix is at the backend layer, not here.

### 3.2 Step-by-step (target: ≤ 12 min from page to edge `/readyz` 200)

#### Step 1 — Detect + declare (0–2 min)

```bash
# Is the proxy container alive?
docker compose -f docker-compose.prod.yml ps caddy

# Is the proxy answering on the edge?
curl -sv https://<host>/readyz 2>&1 | head -20

# Are the backends healthy directly?
curl -sf http://localhost:8000/readyz && echo backend-a-OK
curl -sf http://localhost:8001/readyz && echo backend-b-OK
```

If both backends are healthy and the edge is dead, the proxy layer
is the suspect — proceed.

#### Step 2 — Diagnose + decide (2–5 min)

Read the Caddy logs and decide which fallback path:

```bash
docker compose -f docker-compose.prod.yml logs --tail=200 caddy
```

| Symptom | Path | Reason |
|---|---|---|
| Caddy container restarting / crash-loop / `caddy validate` exits non-zero | A: restart Caddy with last-known-good config | Most common: someone shipped a Caddyfile change that fails parsing |
| Caddy alive but `caddy reload` hangs or returns 5xx for new config | B: revert Caddyfile to git HEAD~1 + `caddy reload` | Bad config landed; revert to the last green commit |
| Caddy container running but no traffic accepted (port bind failure, TLS cert missing) | C: direct-to-backend bypass via host-port exposure | The certificate / port plane is broken at the proxy itself |
| Caddy is fine, the edge fronting it (Cloudflare tunnel, host firewall, k8s ingress) is the broken layer | D: bypass the broken edge layer | Out of OmniSight's runbook scope but covered below for completeness |

#### Step 3a — Path A: restart Caddy (5–7 min)

```bash
# Validate the on-disk Caddyfile FIRST — restarting onto a broken
# config is worse than the current state.
docker compose -f docker-compose.prod.yml exec caddy \
    caddy validate --config /etc/caddy/Caddyfile

# If validate exits 0, restart:
docker compose -f docker-compose.prod.yml restart caddy

# Confirm:
curl -sf https://<host>/readyz && echo edge-OK
```

If `caddy validate` exits non-zero, jump to step 3b.

#### Step 3b — Path B: revert Caddyfile (5–9 min)

```bash
# What was the last committed version of the Caddyfile?
git log --oneline -5 -- deploy/reverse-proxy/Caddyfile

# Revert to the previous commit (NOT a force-push — local checkout
# only; the revert commit follows in §3.3 once we are back).
git checkout HEAD~1 -- deploy/reverse-proxy/Caddyfile

# Reload (zero-downtime — Caddy keeps serving on the old config
# until the new one parses):
docker compose -f docker-compose.prod.yml exec caddy \
    caddy reload --config /etc/caddy/Caddyfile

# Confirm:
curl -sf https://<host>/readyz && echo edge-OK
```

#### Step 3c — Path C: direct-to-backend bypass (5–12 min)

When the proxy itself is unrecoverable in the RTO budget, expose the
backend host ports directly. This **drops TLS at the edge** — only
acceptable as a temporary measure (single-digit hours, with the
incident ticket explicitly noting it).

```bash
# 1. Confirm both backends are answering on their host ports.
curl -sf http://localhost:8000/readyz
curl -sf http://localhost:8001/readyz

# 2. Bring up an emergency unencrypted listener on :80 that load-
#    balances between the two. The shipped fallback Caddyfile lives
#    at deploy/reverse-proxy/Caddyfile — to stand up a minimal
#    bypass listener temporarily, run a foreground caddy with a
#    one-liner config:
docker run --rm -p 80:80 \
    --network host \
    caddy:2-alpine caddy reverse-proxy \
    --from :80 --to localhost:8000 --to localhost:8001

# 3. Notify users that TLS is temporarily unavailable; update DNS
#    or load balancer to point at this bypass host until the proxy
#    is fixed.
```

Once the proxy layer is healed (Caddyfile fixed, container restarted,
TLS cert renewed), follow Path A or Path B to restore the canonical
`:443` / TLS-on edge, then stop the bypass listener.

#### Step 3d — Path D: edge-layer bypass (manual)

If the failure is at a layer **above** Caddy (Cloudflare tunnel
down, k8s ingress controller wedged, host firewall locked out),
this runbook cannot script the fix — the bypass is whatever the
deployment topology supports:

* Bare-VM deploy: re-open `:443` at the host firewall, point DNS
  directly at the host.
* Cloudflare tunnel deploy (`deploy/cloudflared/`): if the tunnel is
  the broken layer, re-open the host's `:443` to the public network
  temporarily and update DNS to bypass the tunnel.
* k8s deploy: the ingress is owned by the cluster; promote a
  NodePort or `kubectl port-forward` as a 60-min stop-gap while the
  ingress is repaired (out of OmniSight's runbook scope — call the
  cluster operator).

#### Step 4 — Verify + acknowledge (12–15 min)

```bash
# Edge alive again?
curl -sf https://<host>/readyz && echo edge-OK
curl -sf https://<host>/api/v1/health && echo health-OK

# (If on Path C bypass) confirm both backends are still serving:
curl -sf http://localhost:8000/readyz
curl -sf http://localhost:8001/readyz
```

Post the "we're back" message, update the ticket, and proceed to
§3.3 (post-action follow-ups).

### 3.3 After the recovery (within the next 24 h, NOT in the RTO budget)

* **If you reverted the Caddyfile** (Path B), open a revert PR with
  the bad commit hash + the failure signature. Do not let the
  reverted change re-land without the regression caught at PR-time.
* **If you used the direct-to-backend bypass** (Path C), restore TLS
  at the edge as soon as the proxy is fixed. Document the bypass
  window in the incident ticket — auditors will ask.
* **Update the incident ticket** with `t₀ → t₁`, which path was used,
  and which step blew (or stayed inside) its budget. Feeds G6 #4.

---

## 4. Topology after both runbook paths

After §2 you have **new primary + nothing** (or new primary + old
primary as a diverged-timeline orphan). After §3 you have **proxy
restored OR bypass active**. The two are orthogonal. The next
operator priorities (NOT inside the 15-min RTO):

* Run §2.3 step "rebuild the old primary as the new standby" within
  24 h — `db_failover.md` §7 is canonical.
* Re-run the G6 #1 daily drill manually with `workflow_dispatch` to
  confirm the **post-incident** topology still passes
  backup → restore → selftest → smoke. A green drill within 1 h of
  recovery is the cheapest evidence the failover did not silently
  break the backup chain.
* If the proxy was on a bypass path, restore TLS-on edge within the
  same shift.

---

## 5. Cross-references

* **G6 #1** — `.github/workflows/dr-drill-daily.yml` + contract
  `backend/tests/test_dr_drill_daily_g6_1.py`. Daily CI evidence
  the restore-execution path holds. Re-run via `workflow_dispatch`
  immediately after invoking §2 to confirm topology.
* **G6 #2** — `docs/ops/dr_rto_rpo.md` + contract
  `backend/tests/test_dr_rto_rpo_g6_2.py`. The RTO ≤ 15 min /
  RPO ≤ 5 min budget this runbook is sized against. Every numbered
  step's budget allocation must sum to §2.2 of that doc.
* **G6 #4** (row 1382, not yet landed) — annual operator-led DR drill
  checklist. MUST reconcile observed RTO / RPO from §2.3 and §3.3
  against the budget.
* **G6 #5** (row 1383, not yet landed) — `scripts/dr_drill.sh`
  operator-runnable wrapper + `docs/ops/dr_runbook.md` aggregator.
  This runbook is one of the artefacts that aggregator will link to.
* **G4 #6** — `docs/ops/db_failover.md`. The full PostgreSQL HA +
  cutover playbook. §2 here extracts the **manual primary-down
  switch** path; for planned failover, rebuild, forensics, sync vs
  async tuning, see G4 #6 §5 / §7 / §8.
* **G3** — `docs/ops/blue_green_runbook.md`. The blue-green ceremony
  the §2 step 4 app re-point can ride on top of.
* **G2** — `deploy/reverse-proxy/Caddyfile` is the source of truth
  for the `:443` listener; `deploy/blue-green/active_upstream.caddy`
  is the symlink the cutover flips. §3 refers to both.
* **G1 / G5 #4** — `/readyz` endpoint; the canonical "service is
  back" signal. See `backend/routers/health.py` + the K8s probe
  contract at `deploy/k8s/10-deployment-backend.yaml`.
* **G7** (rows 1385–1388, not yet landed) — Prometheus + Grafana
  alert rules. The monitoring side that fires the page that
  triggers this runbook.

---

## 6. Scope — what this runbook does NOT cover

* **Automated daily DR drill.** Owned by G6 #1
  (`.github/workflows/dr-drill-daily.yml`). This runbook only covers
  manual operator-driven failover paths.
* **RTO / RPO budget definition.** Owned by G6 #2
  (`docs/ops/dr_rto_rpo.md`). This runbook is sized against those
  numbers — it does not define them.
* **Annual operator-led DR drill checklist.** Owned by G6 #4
  (row 1382, not yet landed).
* **`scripts/dr_drill.sh` wrapper + bundle-closure
  `docs/ops/dr_runbook.md` aggregator.** Owned by G6 #5 (row 1383,
  not yet landed).
* **Observability dashboards / alert rules.** Owned by G7 (rows
  1385–1388, not yet landed).
* **PITR / WAL archival recovery.** Out of scope for OmniSight per
  `docs/ops/db_failover.md` §1.2.
* **Cross-region / multi-standby topology.** Out of scope for
  OmniSight — the HA pair is one primary + one hot standby.
* **Compliance / legal data-retention.** Out of G6 entirely.

If any of the above lands here silently, the contract test's
sibling-scope guards in
`backend/tests/test_dr_manual_failover_g6_3.py` RED-flag it.
