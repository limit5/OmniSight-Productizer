# `schema_drift`

| field | value |
|-------|-------|
| Severity | `DEGRADED` (escalate to `CRITICAL` if writes are visibly failing — see [`audit_write_failed`](audit_write_failed.md)) |
| Source | T5 drift scanner — `scripts/drift_scanner.py` |
| Tier owner | release-eng + db-admin |
| Parent META | OP-721 |

## What triggers it

The drift scanner queries `SELECT version_num FROM alembic_version`
on the live deploy database and compares it with `alembic heads`
in the committed repo. If the values differ — either direction —
it emits `code="schema_drift"`.

Both directions are dangerous:

* **DB ahead of repo** (live `version_num` not in `alembic
  heads`): someone applied a one-off migration manually or a
  branched migration was merged and applied but its file got
  reverted in code.
* **DB behind repo**: a pending migration was committed but never
  applied to prod. New code paths that depend on the new schema
  will start failing — usually surfacing as `audit_write_failed`
  or generic 500s.

## Severity rationale

`DEGRADED` because the drift itself is silent — applications can
keep running on the "wrong" schema for a while before hitting a
broken code path. But the next deploy / migration will fail in a
hard-to-untangle way if drift isn't fixed first.

The scanner does **not** auto-fix `schema_drift` (auto-fix is
advisory only in the current revision). Schema rollforward is a
human decision because the wrong direction loses data.

## Immediate action

1. **Determine direction:**

       psql -h $PGHOST -U $PGUSER -d $PGDB -c \
            'SELECT version_num FROM alembic_version;'
       alembic -c backend/alembic.ini heads

   * Repo head is **ahead** of DB → pending migration.
   * DB is ahead of repo head → unknown migration applied.

2. **If repo is ahead** — apply pending migrations during a
   maintenance window:

       alembic -c backend/alembic.ini upgrade head

   For zero-downtime production runs, follow
   `docs/ops/db_failover.md` §online-migration. Never run a
   schema upgrade without re-checking `alembic_dual_track.py` (it
   gates by-design on dual-track parity).

3. **If DB is ahead** — find the unknown version. Three sources to
   check:

   * `git log --all -- backend/alembic/versions/<unknown>.py` —
     was the file ever in the repo and reverted?
   * Backup snapshots of the DB at known-good points —
     `docs/ops/backup_selftest.py` produces them daily.
   * Manual ALTER history — check `docs/ops/db_matrix.md` for any
     out-of-band schema mods.

   Once the unknown migration is identified, restore its file in
   the repo (preferred) **or** downgrade the DB
   (`alembic ... downgrade <last_known_good>`) and lose any data
   that depended on the unknown schema. Both are last-resort —
   open a postmortem either way.

4. **Re-scan to confirm clean:**

       python scripts/drift_scanner.py --json | jq -e 'all(.[]; .code != "schema_drift")'

## Root-cause investigation

* `docs/ops/upgrade_rollback_ledger.md` — was a recent rollback
  applied to code but not to DB?
* `docs/sop/lessons/` — search for `schema_drift` entries; this code
  has fired before.
* `docs/ops/dependency_upgrade_runbook.md` §alembic — the order of
  operations for migration deploys.

## Escalation

* Schema drift detected, no production errors yet: file under
  release-eng + db-admin, fix within 24h.
* Schema drift **plus** [`audit_write_failed`](audit_write_failed.md)
  or any `OperationalError`/`UndefinedColumnError` in the bridge
  log: escalate to `CRITICAL` immediately; pause writes if
  necessary; open a postmortem.
* Schema drift caused by an unknown manual migration (DB ahead of
  repo): always escalate. This is a process violation —
  `docs/sop/jira-ticket-conventions.md` requires every prod schema
  change to be merged + tagged.
