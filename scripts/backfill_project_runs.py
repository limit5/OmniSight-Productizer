#!/usr/bin/env python3
"""B7 (#207) — backfill project_runs from existing workflow_runs.

Groups workflow_runs into sessions separated by 5-minute gaps.
Idempotent: runs already assigned to a project_run are skipped.

Usage:
    python scripts/backfill_project_runs.py [--gap 300]

SP-5.10 (2026-04-21): project_runs.backfill() is pool-native post-
SP-5.6b. Script now initialises ``backend.db_pool`` before calling
backfill and closes it cleanly on exit. Requires
``OMNISIGHT_DATABASE_URL`` (or ``DATABASE_URL``) in the environment
— no fallback to SQLite from here on.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main(gap: float) -> None:
    from backend import db, project_runs
    from backend import db_pool

    pg_dsn = db._resolve_pg_dsn()
    if not pg_dsn:
        print(
            "ERROR: OMNISIGHT_DATABASE_URL (or DATABASE_URL) must be set "
            "and point at a PostgreSQL instance. SQLite-backed backfill "
            "was removed in SP-5.10.",
            file=sys.stderr,
        )
        sys.exit(2)

    await db_pool.init_pool(
        pg_dsn, min_size=1, max_size=4, command_timeout=60.0, init=None,
    )
    try:
        count = await project_runs.backfill(session_gap_s=gap)
        print(f"Created {count} project_run(s).")
    finally:
        await db_pool.close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill project_runs")
    parser.add_argument("--gap", type=float, default=300.0,
                        help="Session gap in seconds (default: 300)")
    args = parser.parse_args()
    asyncio.run(main(args.gap))
