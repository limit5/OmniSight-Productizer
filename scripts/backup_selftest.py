#!/usr/bin/env python3
"""Backup self-test for the OmniSight SQLite DB.

A backup you've never restored is not a backup. This script does the
whole round-trip in one pass:

  1. Take an online backup (Python's sqlite3.Connection.backup() is
     the WAL-safe equivalent of the `.backup` CLI command).
  2. Open the backup as a fresh DB.
  3. `PRAGMA integrity_check` — must return 'ok'.
  4. Confirm every required table from the OmniSight schema is
     present (schema_rebuild-from-empty would strip tables and pass
     integrity_check — that's why we do both).
  5. Confirm the Phase 53 audit_log hash chain survived the round-
     trip (broken chain = lost tamper-evidence).

Usage:
    python3 scripts/backup_selftest.py                  # data/omnisight.db
    python3 scripts/backup_selftest.py data/prod.db     # explicit path

Exit codes:
    0 — all checks passed
    1 — usage / missing input file
    2 — backup step failed
    3 — integrity_check reported corruption
    4 — sanity query (schema or hash chain) didn't match expectation
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path


REQUIRED_TABLES = (
    "tasks", "agents", "workflow_runs", "workflow_steps",
    "audit_log", "episodic_memory",
)


def log(msg: str) -> None:
    print(f"\033[36m[backup-selftest]\033[0m {msg}")


def fail(code: int, msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def main(argv: list[str]) -> None:
    db_path = Path(argv[1]) if len(argv) > 1 else Path("data/omnisight.db")
    if not db_path.exists():
        fail(1, f"{db_path} not found")

    with tempfile.TemporaryDirectory(prefix="omnisight-restore-") as tmp:
        backup_path = Path(tmp) / "backup.db"

        # ── 1. Online backup ────────────────────────────────────
        log(f"online backup {db_path} → {backup_path}")
        try:
            src = sqlite3.connect(str(db_path))
            try:
                dst = sqlite3.connect(str(backup_path))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except Exception as exc:
            fail(2, f"sqlite3 backup failed: {exc}")

        size = backup_path.stat().st_size
        log(f"backup written: {size} bytes")
        if size < 1024:
            fail(2, f"backup suspiciously small ({size} bytes)")

        # ── 2. Restore = just open the copy. ────────────────────
        conn = sqlite3.connect(str(backup_path))
        conn.row_factory = sqlite3.Row
        try:
            # ── 3. Integrity check ──────────────────────────────
            row = conn.execute("PRAGMA integrity_check;").fetchone()
            if not row or row[0] != "ok":
                fail(3, f"integrity_check: {row[0] if row else '(no row)'}")
            log("integrity_check on restored copy: ok")

            # ── 4. Required tables ──────────────────────────────
            for t in REQUIRED_TABLES:
                count = conn.execute(
                    "SELECT count(*) FROM sqlite_master "
                    "WHERE type='table' AND name = ?",
                    (t,),
                ).fetchone()[0]
                if count != 1:
                    fail(4, f"expected table '{t}' missing from restored DB")
            log(f"all {len(REQUIRED_TABLES)} required tables present")

            for t in REQUIRED_TABLES:
                n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                print(f"  {t:<20s} {n:>8d} rows")

            # ── 5. Audit log hash chain ─────────────────────────
            # Phase 53 guarantees `prev_hash` of row N+1 == `hash` of
            # row N. If the chain is broken on the restored copy we've
            # lost tamper-evidence even if every byte came back.
            has_chain = conn.execute(
                "SELECT count(*) FROM pragma_table_info('audit_log') "
                "WHERE name IN ('hash','prev_hash')",
            ).fetchone()[0]
            if has_chain == 2:
                rows = conn.execute(
                    "SELECT rowid, hash, prev_hash FROM audit_log "
                    "ORDER BY rowid"
                ).fetchall()
                last_hash: str | None = None
                for r in rows:
                    prev = r["prev_hash"]
                    if last_hash is not None and prev is not None and prev != last_hash:
                        fail(4, f"audit_log chain break at rowid={r['rowid']}")
                    last_hash = r["hash"]
                log(f"audit_log hash chain continuous across {len(rows)} rows")
            else:
                log("audit_log has no hash/prev_hash columns — pre-Phase 53, skipping chain check")
        finally:
            conn.close()

    log("backup self-test passed")


if __name__ == "__main__":
    main(sys.argv)
