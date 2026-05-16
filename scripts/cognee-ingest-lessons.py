#!/usr/bin/env python3
"""AUDIT-29b-6 (OP-1024) — ingest lessons + anti-patterns into Cognee.

Loads every ``docs/sop/lessons/L-*.md`` lesson file and every per-pattern
chunk of ``docs/sop/architecture-anti-patterns.md`` into the Cognee
knowledge graph (datasets ``{tenant}:lesson`` / ``{tenant}:antipattern``).
This is the one-shot bootstrap for the lesson-surface meta-mechanism — the
runner's ``_build_prompt`` then retrieves the top-N most relevant entries
per pickup (see ``docs/sop/lessons/L-OP-1024-lesson-surface-meta-mechanism.md``
and ``docs/sop/architecture-anti-patterns.md`` §"Auto-injection").

Usage::

    python3 scripts/cognee-ingest-lessons.py [--dry-run] [--json]
        [--lessons-dir DIR] [--antipatterns-doc PATH]

``--dry-run`` collects + counts the sources but never touches Cognee /
Neo4j — useful for CI smoke + local verification without the KG running.
``--json`` emits a machine-readable report on stdout.

Exit codes:
  0  ok (all sources ingested) or ``--dry-run``
  1  Cognee reachable but some sources failed to ingest (see ``failures``)
  2  Cognee / Neo4j unavailable (package not installed, password default,
     Neo4j down, index corruption / timeout)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents import cognee_integration as ci  # noqa: E402

DEFAULT_LESSONS_DIR = REPO_ROOT / "docs" / "sop" / "lessons"
DEFAULT_ANTIPATTERNS_DOC = REPO_ROOT / "docs" / "sop" / "architecture-anti-patterns.md"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cognee-ingest-lessons.py",
        description="Ingest docs/sop/lessons/*.md + architecture-anti-patterns.md into Cognee",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect + count sources only; do not touch Cognee / Neo4j",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit a JSON report on stdout"
    )
    parser.add_argument(
        "--lessons-dir",
        type=Path,
        default=DEFAULT_LESSONS_DIR,
        help=f"lessons directory (default: {DEFAULT_LESSONS_DIR})",
    )
    parser.add_argument(
        "--antipatterns-doc",
        type=Path,
        default=DEFAULT_ANTIPATTERNS_DOC,
        help=f"anti-patterns cookbook (default: {DEFAULT_ANTIPATTERNS_DOC})",
    )
    return parser.parse_args(list(argv))


def _emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"[cognee-ingest] status={report['status']}")
    print(f"[cognee-ingest] lesson sources seen: {report['lesson_sources_seen']}")
    print(f"[cognee-ingest] anti-pattern sources seen: {report['antipattern_sources_seen']}")
    if "lesson_nodes_ingested" in report:
        print(f"[cognee-ingest] lesson nodes ingested: {report['lesson_nodes_ingested']}")
        print(
            f"[cognee-ingest] anti-pattern nodes ingested: {report['antipattern_nodes_ingested']}"
        )
    for failure in report.get("failures", ()):  # pragma: no cover - prod path
        print(f"[cognee-ingest] FAILED: {failure}", file=sys.stderr)
    if report.get("detail"):
        print(f"[cognee-ingest] detail: {report['detail']}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)

    lesson_sources = ci.collect_lesson_sources(args.lessons_dir)
    antipattern_sources = ci.collect_antipattern_sources(args.antipatterns_doc)

    report: dict = {
        "dry_run": bool(args.dry_run),
        "lessons_dir": str(args.lessons_dir),
        "antipatterns_doc": str(args.antipatterns_doc),
        "lesson_sources_seen": len(lesson_sources),
        "antipattern_sources_seen": len(antipattern_sources),
    }

    if args.dry_run:
        report["status"] = "dry-run"
        _emit(report, args.json)
        return 0

    try:  # pragma: no cover - exercised only with a live Cognee/Neo4j
        adapter = ci.CogneeAdapter.from_env()
    except ci.CogneeNotInstalled as exc:
        report["status"] = "cognee-not-installed"
        report["detail"] = str(exc)
        _emit(report, args.json)
        return 2
    except ci.Neo4jPasswordDefault as exc:
        report["status"] = "neo4j-password-default"
        report["detail"] = str(exc)
        _emit(report, args.json)
        return 2

    try:  # pragma: no cover - exercised only with a live Cognee/Neo4j
        lesson_report = asyncio.run(adapter.ingest(lesson_sources))
        antipattern_report = asyncio.run(adapter.ingest(antipattern_sources))
    except ci.CogneeNeo4jUnavailable as exc:
        report["status"] = "neo4j-unavailable"
        report["detail"] = str(exc)
        _emit(report, args.json)
        return 2
    except (ci.CogneeIndexCorruption, ci.CogneeQueryTimeout) as exc:
        report["status"] = "cognee-error"
        report["detail"] = str(exc)
        _emit(report, args.json)
        return 2

    failures = list(lesson_report.failures) + list(antipattern_report.failures)
    report["lesson_nodes_ingested"] = lesson_report.sources_ingested
    report["antipattern_nodes_ingested"] = antipattern_report.sources_ingested
    report["failures"] = failures
    report["status"] = "ok" if not failures else "partial"
    _emit(report, args.json)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
