#!/usr/bin/env python3
"""B7 (#207) — backfill project_runs from existing workflow_runs.

Groups workflow_runs into sessions separated by 5-minute gaps.
Idempotent: runs already assigned to a project_run are skipped.

Usage:
    python scripts/backfill_project_runs.py [--gap 300]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main(gap: float) -> None:
    from backend import db
    from backend import project_runs

    await db.init()
    try:
        count = await project_runs.backfill(session_gap_s=gap)
        print(f"Created {count} project_run(s).")
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill project_runs")
    parser.add_argument("--gap", type=float, default=300.0,
                        help="Session gap in seconds (default: 300)")
    args = parser.parse_args()
    asyncio.run(main(args.gap))
