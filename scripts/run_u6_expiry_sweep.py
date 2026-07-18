#!/usr/bin/env python3
"""U6-0 GAP-6a operator entrypoint: run the supervised expire-stale sweeper (DEFAULT-OFF, operator-run only).

Does NOTHING unless OMNISIGHT_U6_EXPIRY_SWEEP_ENABLED is truthy.  When enabled it periodically calls db.expire_stale to
expire ABANDONED pending challenges/grants past wall time -- pure hygiene (claim-time expiry fail-closes safety); it
executes NO governed side effect.  No auto-spawn; an operator runs this deliberately.  SIGINT/SIGTERM stop it
gracefully (an interruptible interval sleep wakes on the stop signal).  Exit code: 0 on a clean stop, non-zero if the
sweep circuit-broke (max consecutive errors) so the exit code is an honest liveness signal.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # repo root, so `python scripts/...` finds `backend`

from backend import db_pool                                       # noqa: E402 -- after the repo-root sys.path bootstrap
from backend.db_url import parse as parse_db_url                  # noqa: E402
from backend.agents.expiry_sweeper import expiry_sweep_enabled    # noqa: E402
from backend.agents.expiry_sweeper import run_expiry_sweep_loop   # noqa: E402


def _resolve_asyncpg_dsn(explicit: str) -> str:
    """The asyncpg DSN to connect with, or '' if no candidate is set or the chosen one is syntactically unusable.

    Precedence: explicit --dsn, else OMNISIGHT_DATABASE_URL, else DATABASE_URL.  Each candidate is stripped
    INDEPENDENTLY (mirroring backend.db._resolve_pg_dsn) so a whitespace-only value does not suppress the next
    candidate.  The first NON-empty candidate is authoritative: the project's canonical URL is the SQLAlchemy
    ``postgresql+asyncpg://`` form, which ``asyncpg.create_pool`` rejects, so ``db_url.parse(...).asyncpg_dsn()`` strips
    the ``+asyncpg`` qualifier to the plain ``postgresql://`` form asyncpg wants.  This normalises SYNTAX only: a
    candidate that fails to PARSE, is not Postgres, or is unencodable (a lone-surrogate value -> UnicodeError, a
    ValueError subclass) returns '' so main() prints guidance.  It does NOT validate asyncpg SEMANTICS -- a well-formed
    URL with connect-invalid options (a bad ``sslmode``, or an IPv6-literal host that backend.db_url renders without
    brackets -- a limitation shared by every asyncpg_dsn() caller) is surfaced by asyncpg's own error at CONNECT time,
    not here; the deployment ships a plain hostname/IPv4 DSN.
    """
    for candidate in (explicit, os.environ.get("OMNISIGHT_DATABASE_URL"), os.environ.get("DATABASE_URL")):
        raw = (candidate or "").strip()
        if not raw:
            continue                                             # empty / whitespace-only -> try the next candidate
        try:
            parsed = parse_db_url(raw)
            if not parsed.is_postgres:
                return ""
            return parsed.asyncpg_dsn()  # +asyncpg stripped; parse/UnicodeError caught below
        except ValueError:
            return ""  # parse error (malformed / unsupported) or UnicodeError -> fail closed
    return ""


async def _run(dsn: str, args: argparse.Namespace) -> int:
    await db_pool.init_pool(dsn, min_size=1, max_size=2)
    try:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        async def _interruptible_sleep(seconds: float) -> None:
            # Wake early on SIGINT/SIGTERM; a normal timeout is the ordinary per-interval wait.
            try:
                await asyncio.wait_for(stop.wait(), timeout=seconds)
            except asyncio.TimeoutError:
                return

        stats = await run_expiry_sweep_loop(
            db_pool.get_pool(),
            interval_s=args.interval_s,
            error_backoff_s=args.error_backoff_s,
            max_consecutive_errors=args.max_consecutive_errors,
            should_continue=lambda: not stop.is_set(),
            max_ticks=1 if args.drain else None,
            sleep=_interruptible_sleep,
        )
        print("u6 expiry sweep stopped: %s" % (stats,))
        return 3 if stats.stopped_reason == "circuit_open" else 0
    finally:
        await db_pool.close_pool()


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default="",
        help="PostgreSQL DSN; defaults to OMNISIGHT_DATABASE_URL / DATABASE_URL (postgresql+asyncpg:// accepted).",
    )
    parser.add_argument("--interval-s", type=float, default=60.0)
    parser.add_argument("--error-backoff-s", type=float, default=10.0)
    parser.add_argument("--max-consecutive-errors", type=int, default=5)
    parser.add_argument("--drain", action="store_true", help="one set-wide sweep then exit (else run as a daemon)")
    args = parser.parse_args(argv)
    if not expiry_sweep_enabled():
        print("OMNISIGHT_U6_EXPIRY_SWEEP_ENABLED is not set; nothing to do (default-OFF).")
        return 0
    dsn = _resolve_asyncpg_dsn(args.dsn)
    if not dsn:
        print(
            "error: a PostgreSQL DSN is required when the sweep is enabled "
            "(set OMNISIGHT_DATABASE_URL or pass --dsn postgresql://...)",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run(dsn, args))


if __name__ == "__main__":
    raise SystemExit(main())
