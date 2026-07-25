# DR drill — proving the encrypted backups actually restore

**Ticket:** OP-2733 · **Scope label:** `scope:failed-units-2026-07-25`

## Why a new drill, when two already exist

`scripts/dr_drill.sh` and `scripts/backup_selftest.py` both predate the move to PostgreSQL. Counted
2026-07-25: **12 and 9 references to sqlite respectively, and ZERO to `postgres`, `pg_restore` or
`gpg`.** They exercise a backup mechanism this system no longer uses.

Scheduling them would therefore have produced a permanently green drill that proves nothing about
the artefacts actually being produced — the same looks-configured-but-isn't failure that this whole
sweep exists to remove, and the ticket's own original acceptance criterion said to do exactly that.
It was wrong.

The finding that prompted this: **the 24 encrypted artefacts had never once been restore-tested.**
`grep -rl backup_selftest ~/.config/systemd/user/` returned nothing — the tooling was wired to no
unit at all.

## What `omnisight-dr-drill-pg.sh` proves

On a real artefact, end to end:

1. the `.dump.gpg` decrypts with the stored passphrase;
2. the plaintext carries a `PGDMP` header — a genuine PostgreSQL custom-format dump;
3. it **restores** into a throwaway database — not merely `pg_restore --list`, which proves
   structure but not loadability;
4. the restored database is sane: table count ≥ `DR_MIN_TABLES` (100), an `alembic_version` row is
   present, and `audit_log` is non-empty.

It also warns when the newest encrypted artefact is older than `DR_MAX_AGE_DAYS` (3) — an
independent staleness signal that would have caught the 2026-07-23 backup outage on its own.

## Result of record — 2026-07-25

| artefact | TOC | restore | tables | alembic | audit_log |
|---|---|---|---|---|---|
| `manual-20260629-213551` (oldest) | 734 | OK | 119 | 0251 | 13,058 |
| `manual-20260722-021753` (newest) | 747 | OK | 122 | 0257 | 18,536 |

Both restore. The DR premise holds.

⚠ **But note the alembic gap.** The newest usable encrypted backup is at **0257**, while prod is at
**0280** — v0.8.0 shipped that chain. A restore from it lands a database 23 migrations behind the
running image, and `/readyz` fails closed on image-vs-DB drift, so recovery requires
`alembic upgrade head` after the restore. The three nights the lane was down (07-23 → 07-25) are
exactly the nights that would have captured post-0280 state.

## ⚠ There is no off-site copy of the encrypted backups

`upload_offsite_immutable()` in `backup_prod_db.sh` is a no-op unless `OMNISIGHT_BACKUP_S3_URI` is
set, and in `~/.config/omnisight/backup-dr.env` that line is **commented out**. Every run logs
`[WARN] off-site immutable backup skipped (OMNISIGHT_BACKUP_S3_URI unset)`. So all 24 encrypted
artefacts exist **only on this host**.

The inversion is worth stating plainly: the lane with DLP scanning, gpg encryption and
COMPLIANCE Object Lock available to it stays local, while the *unencrypted, unscanned*
`omnisight-pgdump-s3-daily` lane is the one that leaves the building. A host loss today loses every
encrypted backup and keeps only the unencrypted ones.

This also changes the `--prune 30` question: pruning would be defensible if an immutable off-site
copy existed, and today it does not. Tracked with the lane policy on OP-2731.

## Running it

```sh
omnisight-dr-drill-pg.sh            # newest artefact (what the timer runs)
omnisight-dr-drill-pg.sh --oldest   # oldest — catches passphrase/format drift over time
omnisight-dr-drill-pg.sh --both
```

Scheduled daily at 04:30 by `omnisight-dr-drill.timer`, deliberately clear of the 02:17 backup so a
drill never races a live `pg_dump`. Failure routes to the JIRA alert channel (OP-2728).

## Safety properties

- The decrypted plaintext is shredded by an `EXIT/INT/TERM` trap registered **before** it is
  created, so an abort cannot orphan a decrypted production dump. `backup_prod_db.sh` has the
  inverse bug — its trap covers only the temp database — tracked as **OP-2732**.
- The throwaway database is dropped by the same trap.
- The passphrase is passed on stdin; it never appears in argv or in output.
- The live database is never touched — the restore always targets a fresh database name.
- Verified after the first live run: zero leftover `dr_drill%` databases, zero leftover scratch
  directories.
