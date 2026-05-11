#!/usr/bin/env python3
"""OP-902 (F4) — generate OP-858 failure-graph JSON fixture."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_FIXTURE = Path("/var/lib/omnisight/failure_graph_fixture.json")


def database_url_from_env() -> str:
    url = os.environ.get("OMNISIGHT_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("OMNISIGHT_DATABASE_URL or DATABASE_URL is required")
    return url


def fetch_runner_incidents(engine: Engine) -> list[dict[str, Any]]:
    """Load rows in the existing OP-858 fixture shape."""
    stmt = text(
        """
        SELECT incident_id, ticket_key, failure_class, mutex_label, created_at, summary
        FROM runner_incidents
        WHERE failure_class != 'MEMORY_RECALL_AUDIT'
        ORDER BY created_at ASC, incident_id ASC
        """
    )
    with engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [row_to_fixture_dict(dict(row)) for row in rows]


def row_to_fixture_dict(row: dict[str, Any]) -> dict[str, Any]:
    occurred_at = row["created_at"]
    if hasattr(occurred_at, "isoformat"):
        occurred_at = occurred_at.isoformat()
    occurred_at = str(occurred_at).replace(" ", "T", 1).replace("+00:00", "Z")
    return {
        "incident_id": row["incident_id"],
        "ticket_key": row["ticket_key"],
        "failure_class": row["failure_class"],
        "mutex_label": row.get("mutex_label"),
        "occurred_at": occurred_at,
        "summary": row.get("summary") or "",
    }


def write_fixture(rows: Iterable[dict[str, Any]], path: Path = DEFAULT_FIXTURE) -> int:
    data = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return len(data)


def generate_fixture(engine: Engine, path: Path = DEFAULT_FIXTURE) -> int:
    return write_fixture(fetch_runner_incidents(engine), path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_FIXTURE)
    args = parser.parse_args(argv)

    engine = create_engine(args.database_url or database_url_from_env(), future=True)
    count = generate_fixture(engine, args.output)
    print(f"wrote {count} incident(s) to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
