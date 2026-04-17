# DB Failover & SQLite → PostgreSQL Cutover Runbook (G4 #6 / HA-04)

> Operator-facing runbook for the OmniSight PostgreSQL HA pair and the
> one-shot SQLite → Postgres data cutover. Pairs with
> [`docs/ops/db_matrix.md`](db_matrix.md) (the CI matrix that keeps the
> shim + migrations green every PR),
> [`docs/ops/blue_green_runbook.md`](blue_green_runbook.md) (the app-layer
> cutover the DB cutover rides on top of), and
> [`docs/ops/bootstrap_modes.md`](bootstrap_modes.md) (how
> `OMNISIGHT_DATABASE_URL` / `OMNISIGHT_DATABASE_PATH` flow into the
> backend process).

This runbook is what an oncall reads at 3am when:

1. A planned **SQLite → Postgres cutover** is about to start (§3 / §4).
2. A **planned failover** from primary to hot standby is happening for
   maintenance (§5).
3. A **primary outage** just took the DB down and the standby needs to
   be promoted to take writes (§6).
4. A prior-primary box has been replaced and needs to be re-hydrated
   as the new standby behind the current primary (§7).
5. Something looks wrong with `deploy/postgres-ha/` state and they need
   to read the breadcrumbs without breaking replication (§8 / §10).

Everything below is **script-backed** — every command shown is an exact
copy from `deploy/postgres-ha/*`, `scripts/migrate_sqlite_to_pg.py`, or
`scripts/pg_ha_verify.py`. If a step here disagrees with a script, the
script wins; the contract test in
`backend/tests/test_db_failover_runbook.py` catches drift.

---

## 1. Scope & prerequisites

### 1.1 What this runbook covers

| Scenario | Section | Primitive | Expected duration |
|---|---|---|---|
| SQLite → PG one-shot data cutover | §3 / §4 | `scripts/migrate_sqlite_to_pg.py` | ≈ 2–15 min depending on row count |
| Planned failover (primary maintenance) | §5 | `pg_ctl promote` on standby | ≈ 30 s downtime |
| Unplanned primary outage | §6 | `pg_ctl promote` on standby | ≈ 60 s detect + promote |
| Rebuild old primary as new standby | §7 | `pg_basebackup` via `init-standby.sh` | ≈ 5–20 min depending on DB size |
| State-dir / verifier forensics | §8 / §10 | `scripts/pg_ha_verify.py` | read-only, seconds |

### 1.2 What this runbook does NOT cover

* **PITR / WAL archival.** `archive_mode` is off by default in
  `postgresql.primary.conf`. Opt-in is an operator decision and is
  a G4 follow-up — this runbook only covers streaming replication.
* **Logical replication (CDC).** `wal_level = replica` is the minimum
  for physical streaming; bumping to `logical` is out of scope.
* **Cross-region / multi-standby topologies.** The HA pair in
  `deploy/postgres-ha/docker-compose.yml` is primary + one hot standby.
  Chaining a third node is future work.

### 1.3 Hard prerequisites before any cutover

1. `deploy/postgres-ha/.env` populated with strong
   `POSTGRES_PASSWORD` and `REPLICATION_PASSWORD` (the compose file
   uses the `:?` fail-closed operator — missing env = compose refuses
   to start).
2. `scripts/pg_ha_verify.py` exits 0 on the working tree (75/75 static
   checks green).
3. The `db-engine-matrix` CI job was **green on the merge commit** that
   introduced any Alembic revision newer than `0015`. Hard gate on PG
   15 + 16 since G4 #1; advisory on PG 17 (N7 forward-look). See
   `docs/ops/db_matrix.md`.
4. SQLite source DB has a clean audit-log hash chain
   (`scripts/migrate_sqlite_to_pg.py --dry-run` exits 0 with
   `source_chain_ok: true`). **Do not attempt cutover with a broken
   chain** — the migrator hard-refuses (exit 3).

---

## 2. The files that *are* HA state

`deploy/postgres-ha/` is the committed, load-bearing config bundle.
The runtime-mutable state lives inside the Docker volumes and inside
`PGDATA/`.

### 2.1 Committed config (read-only at runtime)

| File | Role | Who reads it |
|---|---|---|
| `docker-compose.yml` | primary + standby service graph, named volumes, health checks | `docker compose` |
| `postgresql.primary.conf` | WAL + replication knobs on the primary | `pg-primary` container |
| `postgresql.standby.conf` | hot-standby reader knobs on the standby | `pg-standby` container |
| `pg_hba.conf` | scram-sha-256 auth, **both** primary and standby | both containers |
| `init-primary.sh` | first-boot hook — creates `replicator` role + `omnisight_standby_slot` | primary docker-entrypoint-initdb.d |
| `init-standby.sh` | every-boot — pg_basebackup (first) + rewrite `postgresql.auto.conf` | standby container (entrypoint override) |
| `.env.example` | env template, 12 knobs + operator guide for sync replication | operator copies to `.env` |

**Never edit `postgresql.auto.conf` inside `PGDATA` by hand.** It is
rewritten by `init-standby.sh` on every boot. Edit
`postgresql.standby.conf` (static) or the `.env` (primary_conninfo
inputs) instead.

### 2.2 Runtime state (volume-resident)

| Location | Owner | Meaning |
|---|---|---|
| `omnisight-pg-primary` (named volume) | PG primary | `PGDATA/` — authoritative data, WAL, `pg_replication_slots` |
| `omnisight-pg-standby` (named volume) | PG standby | `PGDATA/` — base backup + replayed WAL, `standby.signal`, `postgresql.auto.conf` |
| `pg_replication_slots.omnisight_standby_slot` | PG primary | retained WAL window — guarantees standby can reconnect without `pg_basebackup` |

### 2.3 Invariants the operator must maintain

| Invariant | Why | How broken shows up |
|---|---|---|
| `REPLICATION_SLOT_NAME == 'omnisight_standby_slot'` on both sides | `init-primary.sh` creates it literally; `init-standby.sh` references it in `primary_slot_name` | standby starts but stuck at "slot does not exist" |
| `REPLICATION_APPLICATION_NAME == 'omnisight_standby'` | must match `synchronous_standby_names = 'FIRST 1 (omnisight_standby)'` if sync mode enabled | sync mode never engages — primary never waits |
| `POSTGRES_USER` + `POSTGRES_PASSWORD` identical on primary and standby `.env` | standby has to serve reads of the same app DB after promotion | app errors `authentication failed` after promotion |
| No `docker compose down -v` during normal ops | `-v` deletes the named volumes = total data loss | primary re-initdb's from scratch; standby has to full re-basebackup |

---

## 3. Pre-flight (before any SQLite → Postgres cutover)

Run these in order. Stop at the first failure and fix root cause — do
**not** push through with `OMNISIGHT_*_FORCE` flags unless the trade-off
is documented in the cutover ticket.

### 3.1 Static verify of the deploy bundle

```bash
python3 scripts/pg_ha_verify.py --deploy-dir deploy/postgres-ha
```

Expected: exit 0, "75/75 checks ok". `--json` variant for CI scraping.
Any red check blocks cutover — the deploy bundle is wrong before we
ever touch real data.

### 3.2 Audit-log chain is clean on the SQLite source

```bash
python3 scripts/migrate_sqlite_to_pg.py \
    --source data/omnisight.db --dry-run --json \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
               print('source_chain_ok =', d['source_chain_ok']); \
               sys.exit(0 if d['source_chain_ok'] else 1)"
```

Expected exit 0 + `source_chain_ok: true`. A broken chain means an
existing audit_log row was silently mutated at rest — **that is a
security incident, not a migration problem**. Triage before any
further action:

* `scripts/migrate_sqlite_to_pg.py --dry-run --json | jq
  .source_chain_tenants` lists which tenant's chain broke at which id.
* Do NOT use `--skip-chain-verify` to paper over — that is a dev-only
  escape hatch for a known-broken fixture; in prod it hides evidence.

### 3.3 Target Postgres is up + schema-fresh

```bash
docker compose -f deploy/postgres-ha/docker-compose.yml \
    --env-file deploy/postgres-ha/.env up -d
docker compose -f deploy/postgres-ha/docker-compose.yml ps
# expect: pg-primary (healthy), pg-standby (healthy)
```

Apply the schema with Alembic. The G4 #1 shim translates SQLite idioms
at `before_cursor_execute` time, so no migration edits are needed:

```bash
OMNISIGHT_DATABASE_URL="postgresql+psycopg2://omnisight:${POSTGRES_PASSWORD}@localhost:5432/omnisight" \
    python3 -m alembic -c backend/alembic.ini upgrade head
```

Expected: migration chain runs to `head` (currently `0015`).
Single `t-default` tenant seed row in `tenants` is normal (Alembic
revision `0012`) — the migrator treats that as the sole acceptable
"dirty target" pattern via `ON CONFLICT (id) DO NOTHING` on `tenants`.

### 3.4 Both replicas streaming

On the primary, confirm the standby is attached:

```bash
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-primary \
    psql -U omnisight -d omnisight -c \
    "SELECT application_name, state, sync_state, client_addr
     FROM pg_stat_replication;"
```

Expected: one row with `application_name = omnisight_standby`,
`state = streaming`, and `sync_state ∈ {async, sync}` per §5.3 policy.
Zero rows = standby is NOT attached; fix before cutover (check
`docker compose logs pg-standby`, `pg_replication_slots`, `.env`
passwords).

### 3.5 Operator hot-keys

Open a second terminal and pre-load the rollback command:

```bash
# tab 2 — worst-case wipe + re-run (still non-destructive to SQLite source)
echo "docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-primary psql -U omnisight -c 'TRUNCATE TABLE audit_log, ... CASCADE'"
```

---

## 4. SQLite → Postgres cutover ceremony (§4)

```bash
python3 scripts/migrate_sqlite_to_pg.py \
    --source sqlite:///data/omnisight.db \
    --target postgresql+asyncpg://omnisight:${POSTGRES_PASSWORD}@localhost:5432/omnisight \
    --json --batch-size 500
```

What the script does, step by step (anchored in
`scripts/migrate_sqlite_to_pg.py`):

1. **Resolve + validate URLs.** `--source` must be SQLite; `--target`
   must be `postgresql+asyncpg://`. Any other shape → exit 2.
2. **Source chain pre-flight.** Walk `audit_log` per-tenant and
   recompute `curr_hash = sha256(prev_hash || canonical(payload) ||
   str(round(ts, 6)))` for every row. Any mismatch → exit 3
   **before** any write to the target.
3. **Target sanity check.** Row count per table must be 0 (or exactly
   the `tenants.t-default` seed from Alembic revision `0012`).
   Otherwise exit 4. Opt-out: `--truncate-target` → `TRUNCATE …
   RESTART IDENTITY CASCADE` first.
4. **Copy in fixed FK-safe order.** `TABLES_IN_ORDER` pins `tenants`
   first (other tables FK onto it); everything else deterministic
   for diffable logs.
5. **Preserve `audit_log` byte-for-byte.** `ORDER BY id ASC`, keep
   original `id` / `ts` / `prev_hash` / `curr_hash` — the Merkle chain
   would shatter on any reorder.
6. **IDENTITY sequence realignment.** For `event_log`, `audit_log`,
   `auto_decision_log`, `github_installations` run
   `SELECT setval(pg_get_serial_sequence('<table>', 'id'), MAX(id))`
   so the next implicit insert doesn't collide with a preserved id.
7. **Target chain post-flight.** Re-walk the hash chain on Postgres.
   If the hash rebuilt from target rows doesn't match the source →
   exit 5 (migrator bug, should never happen).
8. **Row-count parity.** Every table must have
   `source_rows == copied_rows` or exit 6.
9. **Structured report.** `--json` prints `MigrationReport.to_dict()`
   with per-table and per-tenant chain telemetry; `--quiet` suppresses
   the human log without losing structure.

### 4.1 Exit codes (`scripts/migrate_sqlite_to_pg.py`)

| Exit | Meaning | Target mutated? | Operator action |
|---|---|---|---|
| **0** | cutover complete, chain verified both sides | yes | promote the app onto the new `DATABASE_URL`; monitor per §4.3 |
| **1** | unexpected / driver unavailable / connection failed | no | read the traceback in the log; typical: `asyncpg` not installed, wrong password, primary down |
| **2** | CLI usage error | no | re-read the `--help` output; most common: SQLite URL passed as `--target` or psycopg2 URL |
| **3** | source audit_log chain broken **before** migration | no | SECURITY: triage which tenant + id broke; do NOT use `--skip-chain-verify` to paper over |
| **4** | target not empty and `--truncate-target` not set | no | decide: was the PG pre-seeded deliberately? If so, pass `--truncate-target`; else you are pointing at the wrong DB |
| **5** | target chain broken **after** migration | yes (but verifiably broken) | migrator bug — capture `--json` output and `docker compose logs pg-primary`, open a P0 issue; do not run the app against this target |
| **6** | row-count mismatch source vs target | yes (partial) | re-run with `--truncate-target`; investigate whether the source is being written to concurrently (cutover should happen on a frozen source) |

### 4.2 Dry run (planning the cutover without touching PG)

```bash
python3 scripts/migrate_sqlite_to_pg.py \
    --source data/omnisight.db --dry-run --json
```

Walks the source, verifies the chain, prints the report — does NOT
connect to any Postgres. Safe to run anytime (it's what the
`pg-live-integration` CI job runs on every PR). Useful for change-review
sign-off ("28 tables, N rows total, all chains green").

### 4.3 Post-cutover monitoring (first 30 min)

| When | Owner | Action |
|---|---|---|
| Immediately after exit 0 | oncall | point the app at `postgresql+asyncpg://…` via `OMNISIGHT_DATABASE_URL` and restart the blue/green standby per `blue_green_runbook.md` §3 |
| T+5 min | oncall | `SELECT COUNT(*) FROM audit_log` on both old SQLite + new PG — numbers must match the migrator's `copied_rows` |
| T+10 min | oncall | `pg_stat_replication.sync_state` is `streaming` and `write_lag/flush_lag/replay_lag` < 1 s |
| T+30 min | oncall | app's `/readyz` + `/api/v1/health` stay 200; audit trail in app UI shows the latest migrated entries |

If any check fails within the first 30 min and you need to roll back
to SQLite: stop the app, restore the previous `OMNISIGHT_DATABASE_URL`
pointing at the SQLite file (which the migrator never touched), and
restart. The migrator is a one-way forward copy — rolling back is a
config swap, not a reverse migration.

---

## 5. Planned failover (primary maintenance)

Use when the current primary needs downtime for host patching, OS
upgrade, or hardware move, and the standby is known-healthy.

### 5.1 Pre-flight

1. `pg_stat_replication.sync_state = sync` (if running synchronous
   mode) — otherwise the standby may be behind.
2. `pg_last_wal_replay_lsn()` on the standby ≥
   `pg_current_wal_lsn()` on the primary, measured twice 10 s apart
   and the gap not growing.
3. App is draining writes or in a maintenance window — otherwise
   the last few seconds of WAL may be lost even on sync mode (sync
   guarantees durability of COMMIT, not liveness of every in-flight
   statement).

### 5.2 Promote ceremony

On the standby host:

```bash
# 1. Snapshot current state — useful forensic if step 3 fails.
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    psql -U omnisight -d omnisight -c \
    "SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn();"

# 2. Promote — writes pg_promote() + removes standby.signal + starts accepting writes.
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    pg_ctl promote -D /var/lib/postgresql/data/pgdata

# 3. Confirm promotion.
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    psql -U omnisight -d omnisight -c "SELECT pg_is_in_recovery();"
# expect: f (false = writes accepted)
```

Then flip the app's `OMNISIGHT_DATABASE_URL` to point at the
(ex-standby) new primary on port 5433 (or swap the host-port mapping
in `docker-compose.yml` if you want the new primary on 5432 — ideally
done during the planned window, not unplanned).

### 5.3 Sync vs async replication — operator policy decision

The HA pair ships with **asynchronous** streaming replication by
default (`synchronous_standby_names = ''` in `postgresql.primary.conf`).
To flip synchronous:

```bash
# Edit deploy/postgres-ha/postgresql.primary.conf:
#   synchronous_standby_names = 'FIRST 1 (omnisight_standby)'
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-primary \
    pg_ctl reload -D /var/lib/postgresql/data/pgdata
```

Choosing between the two is a **policy** decision, not a technical
one. Use this table:

| Dimension | Async (default) | Sync (operator opt-in) |
|---|---|---|
| **Durability on primary crash** | last few hundred ms of WAL may be lost (what was written to primary WAL but not yet shipped to standby) | zero data loss — COMMIT only returns after standby flushed WAL |
| **p50 write latency** | unchanged from single-node (~1–3 ms local) | +2–5 ms local network, +20–50 ms cross-AZ |
| **Standby-down impact on primary** | none — primary keeps accepting writes, WAL accumulates in the replication slot | primary blocks on COMMIT until standby reconnects — can look like an outage |
| **Recommended for** | audit_log, decision_log, sandbox ephemeral writes (high volume, tolerant of ≤1 s loss) | tenant-wide compliance surfaces that need absolute durability guarantee, typically during regulated change windows |
| **How to recover from standby-down under sync** | flip back to async (edit `synchronous_standby_names = ''` + `pg_ctl reload`) OR fix the standby — async flip unblocks writes immediately without data loss | — |

OmniSight's hash-chained `audit_log` already gives tamper-evidence on
a per-row basis — losing the last 300 ms of log under async mode on a
primary crash leaves the chain internally consistent (the missing
row was never hashed into the chain). That is **why async is the
default**: the compliance property doesn't depend on durability of
the last second.

Flip to sync only when the compliance window explicitly demands
zero-loss durability — and flip back to async the moment the window
closes, because the "standby down = primary stops" blast radius is
not worth paying during normal ops.

### 5.4 Exit codes (`pg_ctl promote`)

| Exit | Meaning | Writes accepted? | Operator action |
|---|---|---|---|
| **0** | promoted cleanly | yes, after ≈ 1 s | flip `OMNISIGHT_DATABASE_URL` in the app |
| **non-zero** | promotion failed — typically "server is not in standby mode" (already primary) or "could not write to standby.signal" (permissions) | no | `docker compose logs pg-standby`; if already-primary message, someone promoted already — move on to §7 to rebuild |

---

## 6. Unplanned failover (primary down)

Use when the primary stopped accepting connections and the app is
throwing `connection refused` / `FATAL: the database system is
starting up`.

### 6.1 Decide: transient or persistent?

```bash
docker compose -f deploy/postgres-ha/docker-compose.yml ps
docker compose -f deploy/postgres-ha/docker-compose.yml logs --tail 200 pg-primary
```

If the primary container is `Restarting (…)` with a recoverable error
(disk full, OOM, transient network) and the standby is still
streaming, **fix the primary first** — don't promote. A promote is
one-way: the old primary becomes a diverged timeline that has to be
rebuilt from the new primary.

### 6.2 If the primary will not come back in bounded time

The tolerance threshold is your RTO. For OmniSight default, if the
primary is down for more than ~5 min and the cause is not a
1-command fix, promote.

```bash
# 1. Confirm standby is caught up (or accept the data loss of un-shipped WAL).
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    psql -U omnisight -d omnisight -c \
    "SELECT pg_last_wal_replay_lsn(), pg_last_xact_replay_timestamp();"

# 2. Promote.
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    pg_ctl promote -D /var/lib/postgresql/data/pgdata

# 3. Verify writes.
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    psql -U omnisight -d omnisight -c "SELECT pg_is_in_recovery();"
# expect: f

# 4. App cutover — flip OMNISIGHT_DATABASE_URL and restart one blue/green
#    replica at a time (see blue_green_runbook.md §3-§5).
```

### 6.3 Data-loss accounting (async mode)

After an unplanned promotion in async mode, the following may have
been lost:

* Any write that was COMMITted on the old primary but not yet shipped
  to the standby. Quantifiable from the standby's
  `pg_last_wal_replay_lsn()` vs the old primary's
  `pg_current_wal_lsn()` at the moment of failure — if you can get at
  them. If the primary is unreachable, the loss window is
  `now - pg_last_xact_replay_timestamp()` on the standby.
* Because `audit_log` is Merkle-chained, the **missing tail**
  (unshipped rows) is not forgeable later — the chain on the
  (now-primary) standby ends at the last replicated `curr_hash`. Any
  new row starts from there. Recovering the lost rows from the old
  primary's disk (if recoverable) is a forensic exercise, not part
  of the runbook.

---

## 7. Rebuild the old primary as the new standby

After §5 or §6, the topology is "new primary + nothing". Re-hydrate
the old primary box (or a fresh one) as the new standby.

```bash
# 1. Wipe the old PGDATA — it's a diverged timeline now.
docker compose -f deploy/postgres-ha/docker-compose.yml \
    --env-file deploy/postgres-ha/.env down pg-primary
docker volume rm omnisight-pg-primary   # DELETES old primary data — intentional

# 2. Repurpose the old primary box as a standby by swapping the compose
#    service definitions, OR start a fresh box with the same init-standby.sh
#    pipeline. The clean path is: edit .env so PRIMARY_HOST points at the
#    new primary (ex-standby box), then bring up the new standby container
#    with the init-standby.sh entrypoint.

# 3. First boot runs pg_basebackup from the (now) primary via the named
#    slot. Expected log output:
#   [init-standby] waiting for primary <host>:<port> to accept connections
#   [init-standby] PGDATA empty — running pg_basebackup from <host>
#   [init-standby] pg_basebackup complete
#   [init-standby] handing off to postgres (standby mode, following <host>)

# 4. Verify the new standby attached.
docker compose -f deploy/postgres-ha/docker-compose.yml exec <new-primary> \
    psql -U omnisight -d omnisight -c \
    "SELECT application_name, state FROM pg_stat_replication;"
# expect: omnisight_standby / streaming
```

### 7.1 Why `pg_basebackup` and not `pg_rewind`

`pg_rewind` can sometimes fold a diverged timeline back into the
current primary without a full re-clone — but it requires both ends
to have `wal_log_hints = on` (not currently set) and the rewind
window to overlap the retained WAL. For the OmniSight HA pair, a
full `pg_basebackup` is the **predictable** option: cost is linear
in DB size, outcome is deterministic, no forensic archaeology.

If the DB grows large enough that `pg_basebackup` becomes painful (>30
min), open a runbook update to evaluate `pg_rewind` with
`wal_log_hints = on` — it's a deploy-bundle config change, not a
runtime change.

### 7.2 Slot cleanup

If the old primary had a replication slot that is no longer needed
(the old standby is gone), drop it to free WAL retention pressure:

```bash
docker compose -f deploy/postgres-ha/docker-compose.yml exec <new-primary> \
    psql -U omnisight -d omnisight -c \
    "SELECT pg_drop_replication_slot('<old-slot-name>');"
```

The canonical slot `omnisight_standby_slot` is re-created by
`init-primary.sh` on the *new* primary's first boot — but only if
that box has never booted before. If the new primary is the
ex-standby (which was bootstrapped without an initdb flow), you need
to create the slot manually once:

```bash
docker compose -f deploy/postgres-ha/docker-compose.yml exec <new-primary> \
    psql -U omnisight -d omnisight -c \
    "SELECT pg_create_physical_replication_slot('omnisight_standby_slot')
     WHERE NOT EXISTS (SELECT 1 FROM pg_replication_slots
                       WHERE slot_name = 'omnisight_standby_slot');"
```

This is the single difference between "primary that has run
`init-primary.sh`" and "ex-standby that was promoted to primary".

---

## 8. Forensic / read-only inspection

Safe to run anytime — these never mutate state.

### 8.1 Replication topology snapshot

```bash
# On primary — who is connected and what state they are in.
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-primary \
    psql -U omnisight -d omnisight -c \
    "SELECT pid, application_name, client_addr, state, sync_state,
            pg_wal_lsn_diff(sent_lsn, flush_lsn)   AS flush_lag_bytes,
            pg_wal_lsn_diff(sent_lsn, replay_lsn)  AS replay_lag_bytes
     FROM pg_stat_replication;"

# On primary — is the slot retaining WAL?
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-primary \
    psql -U omnisight -d omnisight -c \
    "SELECT slot_name, active, restart_lsn,
            pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
     FROM pg_replication_slots;"

# On standby — am I still in recovery + how far behind?
docker compose -f deploy/postgres-ha/docker-compose.yml exec pg-standby \
    psql -U omnisight -d omnisight -c \
    "SELECT pg_is_in_recovery(), pg_last_wal_replay_lsn(),
            pg_last_xact_replay_timestamp(),
            now() - pg_last_xact_replay_timestamp() AS replay_lag;"
```

### 8.2 Static verifier of the deploy bundle

```bash
python3 scripts/pg_ha_verify.py --deploy-dir deploy/postgres-ha --json
```

75 checks across compose, conf, hba, init-primary, init-standby,
`.env.example`. Exits 1 if any check is red. Use it before every
operational change — e.g. before enabling sync mode, before swapping
host ports, before rotating the replication password.

---

## 9. CI matrix status cross-reference

The CI matrix at `.github/workflows/db-engine-matrix.yml` is the
**production assertion** that every committed Alembic revision
continues to upgrade green on Postgres. Read this section before
trusting the current HEAD for a cutover.

| Job | Gate | Engines | What it covers |
|---|---|---|---|
| `sqlite-matrix` | hard | SQLite 3.40 + 3.45 | Dual-track `upgrade → downgrade → re-upgrade` on the production engine of the moment |
| `postgres-matrix` | hard on 15 + 16; advisory on 17 | PG 15 + 16 + 17 | Dual-track on PG via the G4 #1 shim; shim drift caught by `test_alembic_pg_compat.py` before reaching this job |
| `pg-live-integration` | hard | PG 16 | `test_alembic_pg_live_upgrade.py` + `migrate_sqlite_to_pg.py --dry-run` smoke against a real PG service container |
| `engine-syntax-scan` | advisory | — | `scripts/check_migration_syntax.py` emits `::warning` per SQLite-only idiom; shim is the translation layer so migrations stay as written |

Cutover readiness = **all four green on the merge commit you plan to
deploy from**. If `postgres-matrix` PG 15 or 16 is red, cutover is
blocked at the source — the shim or a migration has regressed. If
`pg-live-integration` is red, the migrator's `--dry-run` source-chain
verifier regressed; fix before any real cutover.

### 9.1 After cutover — CI follow-up

Once this runbook declares cutover complete for the fleet, the
`db_matrix.md` **G4 handoff status** block lists the follow-ups the
next PR should pick up:

1. Retire `sqlite-matrix` (delete the job from
   `db-engine-matrix.yml`).
2. Promote `postgres: 17` from advisory to hard gate.
3. Flip `scripts/check_migration_syntax.py` to `--strict` in
   `engine-syntax-scan`.
4. Delete `OMNISIGHT_SKIP_FS_MIGRATIONS` (one-shot upgrade action).

These are **not** runbook steps — they are cleanup PRs the next
operator files after watching the new PG topology stay green for a
quarter.

---

## 10. Troubleshooting decision tree

```
docker compose up on the HA pair  →  what state?
│
├── both healthy           normal ops. pg_stat_replication has 1 row, sync_state as configured.
├── primary healthy,       standby cannot attach.
│   standby restarting      ├─ init-standby log says "slot does not exist"
│                           │   → run §7.2 to create omnisight_standby_slot on primary.
│                           ├─ log says "password authentication failed"
│                           │   → REPLICATION_PASSWORD mismatch between primary .env and standby .env.
│                           │     Align both .env files, `docker compose restart pg-standby`.
│                           └─ log says "could not connect to primary"
│                               → PRIMARY_HOST / PRIMARY_PORT wrong in standby .env, OR network blocked.
│
├── primary down,          promotion candidate. follow §6.
│   standby healthy         
│
├── both down              check `docker compose logs`. if volume mount errors → disk/host issue;
│                          if "FATAL: database files are incompatible" → version mismatch between
│                          image and PGDATA (never rebuild the image across a major PG bump
│                          without `pg_upgrade` first).
│
└── split-brain            two primaries simultaneously. one was promoted; the other was
                           brought back without wiping. Pick the one with the most WAL
                           replayed, demote or wipe the other per §7.

scripts/migrate_sqlite_to_pg.py  →  exit ?
│
├── 0   done. target chain verified. app can cutover.
├── 1   driver / connection / unexpected. check traceback.
├── 2   CLI usage. re-read --help.
├── 3   SOURCE chain broken. SECURITY TRIAGE — do NOT --skip-chain-verify.
├── 4   target not empty. pass --truncate-target OR repoint --target.
├── 5   TARGET chain broken. migrator bug. capture --json, open P0.
└── 6   row-count mismatch. source was being written to concurrently, OR network flaked
         mid-copy. re-run with --truncate-target after freezing the source.

pg_ctl promote on standby  →  exit ?
│
├── 0           promoted. pg_is_in_recovery() returns f. app cutover next.
└── non-zero    see §5.4 and §6.
```

---

## 11. Tunables (env vars / conf knobs) cheat-sheet

All defaults are production-safe for the async-replication posture.
Override only with a documented reason (in the runbook ticket).

### 11.1 `deploy/postgres-ha/.env`

| Variable | Default | Effect |
|---|---|---|
| `POSTGRES_USER` | `omnisight` | app superuser; must match on primary + standby |
| `POSTGRES_PASSWORD` | *(required, no default)* | compose refuses to start without it (`:?` fail-closed) |
| `POSTGRES_DB` | `omnisight` | app DB name |
| `REPLICATION_USER` | `replicator` | PG role with `REPLICATION LOGIN` only; no table access |
| `REPLICATION_PASSWORD` | *(required)* | separate from app password; rotated independently |
| `PRIMARY_HOST` | `pg-primary` | what the standby's `primary_conninfo` points at |
| `PRIMARY_PORT` | `5432` | compose-internal port (host port is `PG_PRIMARY_HOST_PORT`) |
| `REPLICATION_SLOT_NAME` | `omnisight_standby_slot` | MUST match `init-primary.sh` literal |
| `REPLICATION_APPLICATION_NAME` | `omnisight_standby` | MUST match `synchronous_standby_names` target |
| `PG_PRIMARY_HOST_PORT` | `5432` | host-side port for primary psql access |
| `PG_STANDBY_HOST_PORT` | `5433` | host-side port for standby read-only queries |
| `STANDBY_BASEBACKUP_TIMEOUT` | `300` | seconds the standby waits for `pg_isready` on the primary before giving up on first boot |

### 11.2 `deploy/postgres-ha/postgresql.primary.conf`

| Knob | Default | When to change |
|---|---|---|
| `wal_level` | `replica` | only bump to `logical` if adding CDC — bumps WAL volume |
| `max_wal_senders` | `10` | raise only when chaining >2 standbys |
| `max_replication_slots` | `10` | raise symmetrically with `max_wal_senders` |
| `wal_keep_size` | `1GB` | raise if standby disconnects frequently for longer than the current retention window |
| `synchronous_commit` | `on` | leave `on` — off loses durability on primary crash |
| `synchronous_standby_names` | `''` | flip to `'FIRST 1 (omnisight_standby)'` per §5.3 policy |
| `archive_mode` | `off` | opt-in for PITR (out of scope this runbook) |

### 11.3 `scripts/migrate_sqlite_to_pg.py`

| Flag | Default | Effect |
|---|---|---|
| `--source` | `OMNISIGHT_DATABASE_PATH` env | SQLite URL or bare path |
| `--target` | — | `postgresql+asyncpg://…` (required unless `--dry-run`) |
| `--batch-size` | `500` | rows per executemany batch |
| `--tables` | *(all)* | comma-separated filter; unknown name → exit 2 |
| `--truncate-target` | off | `TRUNCATE … RESTART IDENTITY CASCADE` before copy |
| `--skip-chain-verify` | off | **DANGEROUS** — dev only; skip the `audit_log` chain walk |
| `--dry-run` | off | walk source + verify chain, no target writes |
| `--json` | off | machine-readable `MigrationReport.to_dict()` output |
| `--quiet` | off | suppress human log (keeps `--json` output) |

---

## 12. Script & contract index

Every artefact this runbook references, with the contract test that
guards it. Ship a fix to any of these → re-run the matching test.

| Layer | Artefact | Contract test | Approx test count |
|---|---|---|---|
| Runtime shim | `backend/alembic_pg_compat.py` | `backend/tests/test_alembic_pg_compat.py` | 226 |
| Live PG upgrade | Alembic head end-to-end | `backend/tests/test_alembic_pg_live_upgrade.py` | 8 |
| DB URL abstraction | `backend/db_url.py` | `backend/tests/test_db_url.py` | 75 |
| HA deploy bundle | `deploy/postgres-ha/*` + `scripts/pg_ha_verify.py` | `backend/tests/test_pg_ha_deployment.py` | 114 |
| Data migration | `scripts/migrate_sqlite_to_pg.py` | `backend/tests/test_migrate_sqlite_to_pg.py` | 51 |
| CI matrix | `.github/workflows/db-engine-matrix.yml` | `backend/tests/test_ci_pg_matrix.py` | 22 |
| This runbook | `docs/ops/db_failover.md` | `backend/tests/test_db_failover_runbook.py` | this row |

---

## 13. Anti-patterns — things this runbook will not tell you to do

* **Editing `PGDATA/postgresql.auto.conf` by hand.** It is rewritten
  by `init-standby.sh` on every boot; your edits disappear after
  `docker compose restart pg-standby`. Edit `.env` inputs or
  `postgresql.standby.conf` instead.
* **Running `docker compose down -v` on the HA pair.** `-v` deletes
  the named volumes = total data loss on primary AND standby. There
  is no backup safety net by default (PITR is off).
* **Skipping the `audit_log` chain check with `--skip-chain-verify`.**
  The flag exists for dev fixtures with deliberately broken chains;
  using it in prod hides exactly the tamper evidence the chain is
  designed to surface.
* **Promoting the standby without first checking
  `pg_stat_replication.sync_state` and replay lag.** In async mode a
  promotion silently loses the unshipped WAL; in sync mode an
  unexpected-unsync standby (lagging, desynced) means the promotion
  is replaying stale data. §5.1 pre-flight covers both.
* **Leaving `synchronous_standby_names` set to
  `'FIRST 1 (omnisight_standby)'` when the standby is known down.**
  Primary COMMITs block forever. Either fix the standby, flip back to
  async (empty `synchronous_standby_names` + `pg_ctl reload`), or
  expect `/api/v1/health` p99 to climb into the seconds.
* **Running the migrator against a live SQLite source being written
  to by the app.** The chain is walked as a snapshot and the row-count
  parity check compares "now" vs "then" — concurrent writes show up
  as exit 6 at best, as a silently-missed row at worst. Freeze the
  source (stop the app's blue replica) before cutover.

---

## 14. Cutover change-management checklist (paste into the ticket)

```
[ ] Pre-flight: scripts/pg_ha_verify.py exits 0 (75/75 checks)
[ ] Pre-flight: migrate_sqlite_to_pg.py --dry-run --json → source_chain_ok: true
[ ] Pre-flight: docker compose ps → both pg-primary + pg-standby healthy
[ ] Pre-flight: alembic upgrade head on the new PG target → exit 0
[ ] Pre-flight: pg_stat_replication shows application_name=omnisight_standby streaming
[ ] Pre-flight: db-engine-matrix CI green on the merge commit
[ ] Freeze:     stop the app write-path (blue replica) OR enter maintenance window
[ ] Cutover:    scripts/migrate_sqlite_to_pg.py --source … --target … --json
[ ] Cutover:    confirm exit 0 + source_chain_ok + target_chain_ok
[ ] App flip:   OMNISIGHT_DATABASE_URL=postgresql+asyncpg://… restart one replica
[ ] Verify:     /readyz 200, audit_log tail matches migrator report
[ ] Monitor:    T+5, T+10, T+30 replication lag + app health (§4.3)
[ ] Post:       file the cutover-sweep PR per §9.1 (retire sqlite-matrix etc.)
```

---

## 15. Cross-references

* **G4 TODO row** — `TODO.md` §G (Postgres cutover).
* **CI matrix runbook** — `docs/ops/db_matrix.md` — pins what the
  four engine jobs mean and when each becomes a hard gate.
* **Bootstrap modes** — `docs/ops/bootstrap_modes.md` — how the app
  picks up `OMNISIGHT_DATABASE_URL` / `OMNISIGHT_DATABASE_PATH`.
* **Blue-green runbook** — `docs/ops/blue_green_runbook.md` — the
  app-layer cutover that rides on top of the DB cutover.
* **I-series multi-tenancy** — `TODO.md` §I — RLS, statement_timeout,
  role-scoped grants all hard-depend on the Postgres posture
  delivered by G4 and documented here.
