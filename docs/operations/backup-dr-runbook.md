# OmniSight Backup + DR Runbook

OP-887 defines the production Postgres backup and restore-test contract:
daily dumps to S3-compatible storage, hourly WAL archive proof, weekly
isolated restore tests, **RTO <= 30 minutes**, and **RPO <= 1 hour**.

## 1. Targets

| Target | Value | Evidence |
|---|---:|---|
| Daily full backup | 02:17 UTC | `backup-daily.timer` |
| WAL archive proof | hourly | `backup-hourly.timer` |
| Restore test | weekly Sunday 03:37 UTC | `backup-restore-test.timer` |
| RTO | 30 minutes | `restore_elapsed_seconds <= 1800` |
| RPO | 1 hour | hourly WAL marker report |

## 2. Host Configuration

Create `/etc/omnisight/backup-dr.env` on the production database host:

```bash
OMNISIGHT_DATABASE_URL=postgres://...
OMNISIGHT_RESTORE_TEST_DATABASE_URL=postgres://...isolated_restore...
OMNISIGHT_BACKUP_S3_URI=s3://omnisight-backups/prod/postgres
OMNISIGHT_BACKUP_S3_ENDPOINT=https://sora.services
OMNISIGHT_BACKUP_DIR=/var/lib/omnisight/backups/postgres
OMNISIGHT_WAL_ARCHIVE_DIR=/var/lib/omnisight/wal-archive
OMNISIGHT_BACKUP_ALERT_WEBHOOK=https://...
OMNISIGHT_BACKUP_RTO_SECONDS=1800
OMNISIGHT_BACKUP_STORAGE_QUOTA_BYTES=214748364800
```

The restore-test database must be isolated from production. The restore
script refuses to run when source and restore URLs are identical.

## 3. Install Timers

```bash
sudo mkdir -p /var/log/omnisight /var/lib/omnisight/backups/postgres
sudo cp deploy/systemd/backup-daily.service /etc/systemd/system/
sudo cp deploy/systemd/backup-daily.timer /etc/systemd/system/
sudo cp deploy/systemd/backup-hourly.service /etc/systemd/system/
sudo cp deploy/systemd/backup-hourly.timer /etc/systemd/system/
sudo cp deploy/systemd/backup-restore-test.service /etc/systemd/system/
sudo cp deploy/systemd/backup-restore-test.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now backup-daily.timer backup-hourly.timer backup-restore-test.timer
```

Verify:

```bash
systemctl list-timers 'backup-*'
systemctl status backup-daily.timer backup-hourly.timer backup-restore-test.timer
```

## 4. Daily Backup

`scripts/backup_postgres_daily.sh` runs `pg_dump --format=custom`, gzip
compresses the dump, writes a SHA-256 manifest, uploads both files to
the configured S3-compatible target, and records
`reports/daily-latest.env`.

Manual run:

```bash
sudo systemctl start backup-daily.service
tail -n 100 /var/log/omnisight/backup-daily.log
```

Success evidence:

```bash
cat /var/lib/omnisight/backups/postgres/reports/daily-latest.env
```

## 5. Hourly WAL Archive

`scripts/backup_wal_hourly.sh` forces a WAL switch, records current LSN
and archive settings, copies WAL files from `OMNISIGHT_WAL_ARCHIVE_DIR`
that changed in the last 70 minutes, packages the archive marker,
uploads it to S3-compatible storage, and records `reports/wal-latest.env`.

Manual run:

```bash
sudo systemctl start backup-hourly.service
tail -n 100 /var/log/omnisight/backup-hourly.log
```

Production Postgres must also have WAL archiving enabled. Minimum
database settings:

```conf
archive_mode = on
archive_timeout = 1h
archive_command = 'test ! -f /var/lib/omnisight/wal-archive/%f && cp %p /var/lib/omnisight/wal-archive/%f'
```

## 6. Weekly Restore Test

`scripts/restore_test_weekly.sh` selects the newest daily dump, verifies
its SHA manifest, restores into `OMNISIGHT_RESTORE_TEST_DATABASE_URL`,
compares table row-count snapshots between source and restore, and
writes:

* `reports/restore-test-latest.md`
* `reports/restore-test-latest.diff`

Manual run:

```bash
sudo systemctl start backup-restore-test.service
tail -n 100 /var/log/omnisight/backup-restore-test.log
cat /var/lib/omnisight/backups/postgres/reports/restore-test-latest.md
```

The service timeout is 1800 seconds. A run exceeding 30 minutes fails
the service and alerts as `RestoreTestFailed`.

## 7. Alerts and Error Catalog

| Error | Script behavior | Operator action |
|---|---|---|
| `BackupUploadFailed` | retry 3 times, then alert | Check S3 endpoint, credentials, and network. Rerun the failed service after fixing. |
| `RestoreTestFailed` | alert and optionally open P1 ticket when `OMNISIGHT_JIRA_REST_URL` and `OMNISIGHT_JIRA_AUTH_HEADER` are set | Keep the failed artefacts, inspect diff, rerun from previous daily dump if corruption is suspected. |
| `StorageQuotaExceeded` | purge oldest local backup artefacts until under quota, alert each purge | Increase storage or confirm retention remains acceptable. |

Backup miss alerting is handled by the hourly WAL job. Each hourly run
checks `reports/daily-latest.env`; if the daily report is missing or
older than 26 hours, it posts `BackupMissed` through
`OMNISIGHT_BACKUP_ALERT_WEBHOOK` and writes the same code to the hourly
log. A failed service also leaves a non-zero systemd state and writes
the catalog code to `/var/log/omnisight/backup-*.log`.

## 8. Full Restore Procedure

1. Declare incident and start the 30-minute RTO timer.
2. Stop application writes to the failed primary.
3. Select the newest daily dump whose SHA manifest verifies.
4. Provision a fresh Postgres instance matching the production major
   version.
5. Decompress the dump and restore:

   ```bash
   gzip -dc daily-YYYYmmddTHHMMSSZ.dump.gz > restore.dump
   pg_restore --dbname="$RESTORED_DATABASE_URL" --clean --if-exists --no-owner --no-privileges restore.dump
   ```

6. Apply available WAL archive according to the incident recovery target.
7. Point the application database URL at the restored primary.
8. Run readiness checks and record elapsed seconds.

## 9. Rollback

The scripts are idempotent: rerunning a backup creates a new timestamped
object, and rerunning a restore test rebuilds the isolated restore
schema. Roll back timer installation with:

```bash
sudo systemctl disable --now backup-daily.timer backup-hourly.timer backup-restore-test.timer
sudo rm -f /etc/systemd/system/backup-daily.{service,timer}
sudo rm -f /etc/systemd/system/backup-hourly.{service,timer}
sudo rm -f /etc/systemd/system/backup-restore-test.{service,timer}
sudo systemctl daemon-reload
```

Do not delete existing backup objects during rollback unless the
incident commander explicitly approves retention changes.
