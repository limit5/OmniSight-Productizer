# OmniSight Disaster Recovery — Postgres Backup and Restore

> OP-776 defines the production backup and disaster-recovery contract
> for the Postgres HA deployment. The target is **RTO ≤ 1h** and
> **RPO ≤ 1h** for full restore plus smoke.

This runbook is the operator path for a production database-loss event:
scheduled Postgres base backups, WAL archival, offsite copy with SHA
verification, weekly restore tests, retention, and the full restore
ceremony. It complements the older G6 SQLite/local-host drill docs under
`docs/ops/`; for OP-776, this file is the canonical Postgres backup and
restore runbook.

## 1. Objectives

| Objective | Target | Evidence path |
|---|---:|---|
| **RTO** (Recovery Time Objective) | ≤ 1h | Weekly restore-test job timeout and `restore_elapsed_seconds` report metric |
| **RPO** (Recovery Point Objective) | ≤ 1h | Hourly WAL archive cadence plus daily `pg_basebackup` snapshot |
| Full restore proof | weekly | `.github/workflows/postgres-backup-dr.yml` weekly cron |
| Offsite copy proof | every daily / weekly run | SHA-256 manifest checked after offsite copy |

## 2. Scheduled Backup Plan

The checked-in schedule lives in
`.github/workflows/postgres-backup-dr.yml` and uses these cron entries:

| Cadence | Cron | What it proves |
|---|---|---|
| Hourly | `7 * * * *` | WAL archival cadence; `pg_switch_wal()` proof is hashed into the hourly artefact. |
| Daily | `17 2 * * *` | Full `pg_basebackup --wal-method=stream` snapshot. |
| Weekly | `37 3 * * 0` | Restore latest full snapshot into an ephemeral Postgres instance and run smoke SQL. |

Production host cron or systemd timers may wrap the same commands, but
the cadence must not be looser than the three entries above.

## 3. Backup Primitives

Use physical backups:

```bash
pg_basebackup \
  --host "$PGHOST" \
  --port "$PGPORT" \
  --username "$PGUSER" \
  --pgdata "$BACKUP_ROOT/basebackup" \
  --format=tar \
  --wal-method=stream \
  --gzip \
  --checkpoint=fast \
  --progress
```

For the hourly WAL proof, force a segment switch and archive the current
LSN metadata:

```bash
psql -v ON_ERROR_STOP=1 -Atc "SELECT pg_switch_wal();" > wal-hourly/switched-lsn.txt
psql -v ON_ERROR_STOP=1 -Atc "SELECT pg_current_wal_lsn();" > wal-hourly/current-lsn.txt
sha256sum wal-hourly/*.txt > wal-hourly/SHA256SUMS
```

Production Postgres should also enable WAL archiving on the primary
using an operator-managed archive destination. Keep the archive command
simple and fail-closed; do not discard WAL on copy errors:

```conf
archive_mode = on
archive_timeout = 1h
archive_command = 'test ! -f /backup/wal/%f && cp %p /backup/wal/%f'
```

## 4. Offsite Copy and Encryption at Rest

Backups must be stored in two places:

1. Local backup directory on the database host.
2. Offsite S3 or equivalent object storage.

The workflow uses a local `OFFSITE_ROOT` directory as an S3-equivalent
mirror for CI. Production operators should set an object-store target
with encryption at rest enabled. S3 examples:

```bash
aws s3 cp "$BACKUP_ROOT/basebackup/base.tar.gz" "$OMNISIGHT_DR_OFFSITE_URI/base.tar.gz" \
  --sse AES256
aws s3 cp "$BACKUP_ROOT/basebackup/SHA256SUMS" "$OMNISIGHT_DR_OFFSITE_URI/SHA256SUMS" \
  --sse AES256
```

Every offsite copy must be verified by SHA before the run is considered
green:

```bash
sha256sum "$BACKUP_ROOT/basebackup/"*.tar.gz > "$BACKUP_ROOT/basebackup/SHA256SUMS"
aws s3 cp "$OMNISIGHT_DR_OFFSITE_URI/base.tar.gz" /tmp/op776-verify/base.tar.gz
aws s3 cp "$OMNISIGHT_DR_OFFSITE_URI/SHA256SUMS" /tmp/op776-verify/SHA256SUMS
(cd /tmp/op776-verify && sha256sum --check SHA256SUMS)
```

Do not treat upload success as backup success unless the SHA check also
passes.

## 5. Retention Policy

Maintain these minimum retention windows:

| Backup class | Minimum retention |
|---|---:|
| Hourly WAL / hourly proof artefacts | 7 days |
| Daily full snapshots | 30 days |
| Monthly full snapshots | 12 months |

Object storage lifecycle rules may move older objects to colder tiers,
but they must preserve restore availability for the full window.

## 6. Weekly Restore-Test Cron

The weekly cron restores the latest backup to an ephemeral Postgres
instance and runs smoke SQL:

1. Download or mirror the latest backup plus `SHA256SUMS`.
2. Run `sha256sum --check SHA256SUMS`.
3. Extract `base.tar.gz` into a fresh Postgres data directory.
4. Start `postgres:16-alpine` on an isolated restore port.
5. Run `pg_isready`.
6. Run the smoke query:

```sql
SELECT count(*)
FROM op776_smoke
WHERE payload = 'op-776 synthetic drill';
```

The run must finish under 60 minutes. The workflow records
`restore_elapsed_seconds` so the achieved RTO can be compared with the
1h target.

## 7. Synthetic DR Drill

For a full synthetic drill, use the workflow dispatch mode
`synthetic-dr-drill` in `.github/workflows/postgres-backup-dr.yml`.
It simulates production database loss by:

1. Seeding a source Postgres database.
2. Taking a `pg_basebackup --wal-method=stream` backup.
3. Copying it to the offsite mirror and verifying SHA.
4. Restoring into an ephemeral Postgres instance.
5. Running `pg_isready` and the smoke SQL.
6. Failing the job if elapsed restore time exceeds 1h.

Attach the resulting `op776-postgres-dr-report.md` artefact to the
incident or drill ticket.

## 8. Full Restore During Production DB Loss

1. Declare the incident and start the RTO timer.
2. Stop application writes to the failed primary.
3. Select the latest full snapshot whose SHA manifest verifies.
4. Provision a fresh Postgres instance matching the production major
   version.
5. Extract the base backup into the fresh `PGDATA`.
6. Apply available WAL from the archive up to the desired recovery
   target.
7. Start Postgres and wait for `pg_isready`.
8. Run smoke:

```bash
pg_isready -h "$RESTORED_PGHOST" -p "$RESTORED_PGPORT" -U "$PGUSER" -d "$PGDATABASE"
psql -h "$RESTORED_PGHOST" -p "$RESTORED_PGPORT" -U "$PGUSER" -d "$PGDATABASE" \
  -v ON_ERROR_STOP=1 -c "SELECT 1;"
```

9. Point the backend `OMNISIGHT_DATABASE_URL` at the restored primary.
10. Confirm `/readyz` is 200 and record the elapsed time.

If any step cannot complete inside 1h, mark the drill or incident as an
RTO miss and keep the ticket open until the bottleneck has an owner.

## 9. Failure Handling

| Failure | Action |
|---|---|
| `pg_basebackup` fails | Treat as backup pipeline red; check replication permissions, disk space, and Postgres health before retrying. |
| SHA verification fails | Delete the offsite object copy, keep the local backup, rerun upload once, and escalate if the second SHA check fails. |
| Restore instance does not become ready | Keep artefacts, collect container logs, and run the previous daily snapshot to distinguish corrupt backup from restore-host failure. |
| Smoke query fails | Treat as restore-test failure; do not claim RTO/RPO green for that week. |

## 10. Evidence to Record

For every weekly drill and real incident, record:

* Backup timestamp and object key.
* SHA-256 manifest result.
* Restore start and end timestamp.
* `restore_elapsed_seconds`.
* Smoke command and result.
* Whether RTO ≤ 1h and RPO ≤ 1h were met.
