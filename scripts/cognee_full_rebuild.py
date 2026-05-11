#!/usr/bin/env python3
"""OP-852 nightly Cognee KG full-rebuild entrypoint.

Replays the full ECL pipeline from HEAD against a Cognee + Neo4j stack.
Designed to be invoked on a 24h cron (per AC #2 + #7) and as the
recovery procedure when ``CogneeIndexCorruption`` is raised. The
underlying ``cognee.add`` is per-identifier idempotent so a full
rebuild is safe to re-run without quiescing writers.

Usage
-----
    # Nightly cron (full rebuild from HEAD):
    python -m scripts.cognee_full_rebuild --repo-root /app

    # Dry-run — collect sources but skip the Cognee write path:
    python -m scripts.cognee_full_rebuild --repo-root /app --dry-run

    # Override lessons dir (e.g. for staging tenant):
    python -m scripts.cognee_full_rebuild \
        --repo-root /app \
        --lessons-dir /app/docs/sop/lessons

Environment
-----------
Reads connection info from ``OMNISIGHT_COGNEE_NEO4J_*`` (see
``backend.agents.cognee_integration.CogneeConfig``). Default targets
the ``neo4j`` service from the ``cognee`` compose profile.

Exit codes
----------
* 0 — rebuild succeeded
* 2 — Cognee package or Neo4j unavailable; rebuild skipped (operator
  should investigate per ``docs/operations/cognee-runbook.md``)
* 1 — unexpected failure (stack trace logged)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from backend.agents.cognee_integration import (
    CogneeAdapter,
    CogneeNeo4jUnavailable,
    CogneeNotInstalled,
    collect_code_sources,
    collect_lesson_sources,
    run_ecl_pipeline,
)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cognee_full_rebuild",
        description="Nightly Cognee KG full rebuild from HEAD (OP-852 §3.4).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current working directory)",
    )
    parser.add_argument(
        "--lessons-dir",
        type=Path,
        default=None,
        help="Lessons directory (default: <repo>/docs/sop/lessons)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect sources but do not call Cognee ingest.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("cognee_full_rebuild")

    repo_root = args.repo_root.resolve()
    lessons_dir = (args.lessons_dir or repo_root / "docs" / "sop" / "lessons").resolve()

    if args.dry_run:
        # Drives the source-collection path so cron monitoring can still
        # exercise the inputs without touching the KG store.
        code_count = len(collect_code_sources(repo_root))
        lesson_count = len(collect_lesson_sources(lessons_dir))
        log.info(
            "dry-run: would re-ingest code=%s lessons=%s from %s",
            code_count,
            lesson_count,
            repo_root,
        )
        return 0

    try:
        adapter = CogneeAdapter.from_env()
    except CogneeNotInstalled as exc:
        log.error("cognee package not installed; skipping rebuild (%s)", exc)
        return 2
    except CogneeNeo4jUnavailable as exc:
        log.error("neo4j unavailable; skipping rebuild (%s)", exc)
        return 2

    try:
        report = asyncio.run(
            run_ecl_pipeline(
                repo_root,
                lessons_dir=lessons_dir,
                adapter=adapter,
            )
        )
    except CogneeNeo4jUnavailable as exc:
        log.error("neo4j became unavailable mid-rebuild (%s)", exc)
        return 2
    except Exception:  # noqa: BLE001 — top-level entrypoint surfaces anything else
        log.exception("cognee full rebuild failed")
        return 1

    log.info(
        "cognee full rebuild complete: "
        "code=%s lessons=%s jira=%s gerrit=%s total=%s failures=%s",
        report.code.sources_ingested,
        report.lessons.sources_ingested,
        report.jira.sources_ingested,
        report.gerrit.sources_ingested,
        report.total_ingested,
        len(report.code.failures)
        + len(report.lessons.failures)
        + len(report.jira.failures)
        + len(report.gerrit.failures),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
