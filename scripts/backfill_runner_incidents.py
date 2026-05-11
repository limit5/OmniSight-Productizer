#!/usr/bin/env python3
"""OP-902 (F4) — backfill ``runner_incidents`` from runner log archives."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.agents.runner_log_parser import (  # noqa: E402
    ParsedRunnerIncident,
    iter_log_files,
    parse_log_files,
)
from scripts.generate_failure_graph_fixture import DEFAULT_FIXTURE, generate_fixture  # noqa: E402

DEFAULT_LOG_DIR = Path("/home/user/work/sora/logs/runner")


def database_url_from_env() -> str:
    url = os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("OMNISIGHT_DATABASE_URL or DATABASE_URL is required")
    return url


def insert_incidents(engine: Engine, incidents: list[ParsedRunnerIncident]) -> tuple[int, int]:
    """Insert incidents idempotently; returns ``(inserted, skipped)``."""
    stmt = text(
        """
        INSERT INTO runner_incidents (
            incident_id, ticket_key, failure_class, summary, raw_traceback,
            runner_class, mutex_label, area, created_at
        )
        VALUES (
            :incident_id, :ticket_key, :failure_class, :summary, :raw_traceback,
            :runner_class, :mutex_label, :area, :created_at
        )
        ON CONFLICT (incident_id) DO NOTHING
        """
    )
    inserted = 0
    skipped = 0
    with engine.begin() as conn:
        for idx, incident in enumerate(incidents, 1):
            result = conn.execute(stmt, _params(incident))
            if result.rowcount:
                inserted += 1
            else:
                skipped += 1
            if idx % 100 == 0:
                print(f"progress: processed={idx} inserted={inserted} skipped={skipped}")
    return inserted, skipped


def _params(incident: ParsedRunnerIncident) -> dict[str, object]:
    return {
        "incident_id": incident.incident_id,
        "ticket_key": incident.ticket_key,
        "failure_class": incident.failure_class.value,
        "summary": incident.summary,
        "raw_traceback": incident.raw_traceback,
        "runner_class": incident.runner_class,
        "mutex_label": incident.mutex_label,
        "area": None,
        "created_at": incident.timestamp,
    }


def count_runner_incidents(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(conn.execute(text("SELECT count(*) FROM runner_incidents")).scalar_one())


def backfill(
    *,
    engine: Engine,
    log_dir: Path = DEFAULT_LOG_DIR,
    fixture_path: Path = DEFAULT_FIXTURE,
) -> dict[str, object]:
    paths = iter_log_files(log_dir)
    incidents = parse_log_files(paths)
    inserted, skipped = insert_incidents(engine, incidents)
    fixture_count = generate_fixture(engine, fixture_path)
    distribution = Counter(incident.failure_class.value for incident in incidents)
    total = count_runner_incidents(engine)
    return {
        "files": len(paths),
        "parsed": len(incidents),
        "inserted": inserted,
        "skipped": skipped,
        "fixture_count": fixture_count,
        "total": total,
        "distribution": dict(sorted(distribution.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url or database_url_from_env(), future=True)
    report = backfill(engine=engine, log_dir=args.log_dir, fixture_path=args.fixture)
    print(
        "runner_incidents backfill complete: "
        f"files={report['files']} parsed={report['parsed']} "
        f"inserted={report['inserted']} skipped={report['skipped']} "
        f"fixture={report['fixture_count']} total={report['total']}"
    )
    print(f"failure_class distribution: {report['distribution']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
