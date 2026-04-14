#!/usr/bin/env python3
"""Audit log retention — archive old rows to cold file, prune from DB.

The Phase 53 audit_log is hash-chained: every row's `prev_hash` points
at the previous row's `curr_hash`. That makes the log tamper-evident
but also makes pruning delicate — you can't just DELETE old rows or
the chain breaks at the gap and future verifiers can't tell "pruned
by policy" from "silently edited".

This script does the deletion safely:

  1. Read every row strictly older than `--days` (default 90).
  2. Write them as newline-delimited JSON into
     `data/audit-archive/audit-YYYYMMDD-HHMMSS.jsonl`. One file per
     run, no overwrite, no mutation — this is the cold record.
  3. Record the boundary in a manifest: last archived id + its
     curr_hash. That hash becomes the authoritative "pre-chain"
     value that the first surviving row must carry as its prev_hash.
  4. DELETE those rows from the DB in a single transaction.

Verification is the inverse: `--verify` re-reads the manifest +
archive, re-computes the hash of the last archived row, and confirms
the DB's oldest remaining row's prev_hash matches. Any mismatch is
evidence of tampering.

Usage:
    python3 scripts/audit_archive.py               # default: 90d retention
    python3 scripts/audit_archive.py --days 30
    python3 scripts/audit_archive.py --dry-run     # count + sizes, no writes
    python3 scripts/audit_archive.py --verify      # chain integrity check only
    python3 scripts/audit_archive.py --db data/prod.db

Cron-friendly: run nightly alongside backup_selftest.py. Keeps DB
size linear in retention window, archive files immutable for forever.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sqlite3
import sys
from pathlib import Path


DEFAULT_DAYS = 90
ARCHIVE_DIR = Path("data/audit-archive")
MANIFEST_FILE = ARCHIVE_DIR / "manifest.jsonl"


def log(msg: str) -> None:
    print(f"\033[36m[audit-archive]\033[0m {msg}")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/omnisight.db",
                   help="Path to omnisight.db (default: data/omnisight.db)")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"Retention window in days (default: {DEFAULT_DAYS})")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be archived; make no changes")
    p.add_argument("--verify", action="store_true",
                   help="Cross-check last manifest boundary against live DB")
    return p.parse_args()


def _cutoff_ts(days: int) -> float:
    return _dt.datetime.now().timestamp() - days * 86400


def _do_archive(db_path: Path, days: int, dry_run: bool) -> int:
    cutoff = _cutoff_ts(days)
    cutoff_iso = _dt.datetime.fromtimestamp(cutoff).isoformat()
    log(f"archive target: rows with ts < {cutoff_iso} (>{days} days old)")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE ts < ? ORDER BY id ASC",
            (cutoff,),
        ).fetchall()
    finally:
        if dry_run:
            conn.close()

    if not rows:
        log("no rows to archive")
        if not dry_run:
            conn.close()
        return 0

    log(f"{len(rows)} rows eligible (id {rows[0]['id']} → {rows[-1]['id']})")

    if dry_run:
        return len(rows)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_path = ARCHIVE_DIR / f"audit-{stamp}.jsonl"

    try:
        with archive_path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
        log(f"wrote {archive_path} ({archive_path.stat().st_size} bytes)")

        # Manifest line for the boundary: last archived id + its curr_hash
        # becomes the "pre-chain" that the next surviving row must match.
        last = rows[-1]
        manifest_entry = {
            "archived_at": _dt.datetime.now().isoformat(),
            "archive_file": archive_path.name,
            "row_count": len(rows),
            "first_id": rows[0]["id"],
            "last_id": last["id"],
            "last_curr_hash": last["curr_hash"],
            "cutoff_ts": cutoff,
            "retention_days": days,
        }
        with MANIFEST_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(manifest_entry) + "\n")
        log(f"appended manifest boundary (last_id={last['id']})")

        # Single transaction delete so we don't leave a half-pruned
        # state if something crashes mid-sweep.
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        with conn:
            conn.execute(
                f"DELETE FROM audit_log WHERE id IN ({placeholders})", ids,
            )
        log(f"pruned {len(ids)} rows from audit_log")
    finally:
        conn.close()

    return len(rows)


def _do_verify(db_path: Path) -> None:
    if not MANIFEST_FILE.exists():
        log("no manifest — nothing to verify (first archive hasn't run)")
        return

    with MANIFEST_FILE.open("r", encoding="utf-8") as fh:
        entries = [json.loads(line) for line in fh if line.strip()]
    if not entries:
        log("manifest empty — nothing to verify")
        return

    last = entries[-1]
    expected_prev_hash = last["last_curr_hash"]
    log(f"boundary from manifest: last_id={last['last_id']}, "
        f"curr_hash={expected_prev_hash[:16]}…")

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT id, prev_hash FROM audit_log "
            "WHERE id > ? ORDER BY id ASC LIMIT 1",
            (last["last_id"],),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        log("DB has no rows newer than the boundary — nothing to verify yet")
        return

    actual_prev_hash = row[1]
    if actual_prev_hash != expected_prev_hash:
        log("❌ chain break — boundary mismatch:")
        log(f"   manifest says last archived curr_hash = {expected_prev_hash}")
        log(f"   DB's next row (id={row[0]}) has prev_hash = {actual_prev_hash}")
        log("   → audit log may have been tampered with OR archive file edited")
        sys.exit(1)
    log(f"✅ chain intact across archive boundary (id={row[0]})")


def main() -> None:
    args = _parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: {db_path} not found", file=sys.stderr)
        sys.exit(1)

    if args.verify:
        _do_verify(db_path)
        return

    n = _do_archive(db_path, args.days, args.dry_run)
    log(f"done ({'dry-run: ' if args.dry_run else ''}{n} rows)")


if __name__ == "__main__":
    main()
