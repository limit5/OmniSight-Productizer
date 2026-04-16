"""O8 (#271) — CLI helper for draining in-flight distributed dispatches.

Backs the rollback runbook at ``docs/ops/orchestration_migration.md §2``.

Usage::

    python -m backend.orchestration_drain --strategy wait --wait-s 600
    python -m backend.orchestration_drain --strategy redispatch_monolith \\
        --wait-s 120

Output is a single-line JSON ``DrainReport`` on stdout — easy to pipe
into ``jq`` or capture in an ops ticket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from backend.orchestration_mode import drain_distributed_inflight


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m backend.orchestration_drain",
        description=(
            "Drain this orchestrator's in-flight distributed dispatches "
            "during a monolith rollback."
        ),
    )
    p.add_argument(
        "--strategy",
        choices=("wait", "redispatch_monolith"),
        default="wait",
        help=(
            "wait: poll the queue for natural termination (use when the "
            "worker pool is still up). redispatch_monolith: re-run each "
            "still-pending dispatch through the monolith path (use when "
            "the worker pool is going away)."
        ),
    )
    p.add_argument(
        "--wait-s",
        type=float,
        default=300.0,
        help="How long to wait for natural termination before giving up.",
    )
    p.add_argument(
        "--poll-interval-s",
        type=float,
        default=0.5,
        help="Queue poll interval in seconds.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    report = asyncio.run(drain_distributed_inflight(
        strategy=args.strategy,
        wait_s=args.wait_s,
        poll_interval_s=args.poll_interval_s,
    ))
    print(json.dumps(report.to_dict(), indent=2))
    # Exit non-zero if anything is still pending so CI / ops scripts can
    # react.
    return 0 if not report.still_pending else 2


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
