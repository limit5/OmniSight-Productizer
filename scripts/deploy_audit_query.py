#!/usr/bin/env python3
"""Query deploy_audit in bounded pages for operator change review.

Usage:
    python3 scripts/deploy_audit_query.py --since=30d
    python3 scripts/deploy_audit_query.py --since=30d --kind=deploy
    python3 scripts/deploy_audit_query.py --since=90d --format=csv

The script is read-only and idempotent. It talks directly to the
SQLite deploy_audit database used by local drills. Production operators
can point it at an exported sqlite copy with --db, or use the HTTP CSV
surface documented in the master runbook when querying live prod.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable


DEFAULT_DB = Path("deploy_audit.db")
COLUMNS = (
    "id",
    "ts",
    "kind",
    "tag",
    "actor",
    "reason",
    "status",
    "elapsed_seconds",
    "context",
    "prev_hash",
    "curr_hash",
)
KINDS = {"deploy", "rollback", "slo_breach", "operator_action"}


class AuditQueryTimeout(RuntimeError):
    """Raised when sqlite cannot complete a page within the timeout."""


def _parse_since(value: str) -> dt.datetime:
    match = re.fullmatch(r"(\d+)([dhw])", value.strip())
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        days = amount if unit == "d" else amount * 7 if unit == "w" else 0
        hours = amount if unit == "h" else 0
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=days, hours=hours
        )
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--since must be like 30d, 12h, 4w, or ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _parse_until(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
    return _parse_since(value)


def _sqlite_ts(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def _rows(
    conn: sqlite3.Connection,
    *,
    since: dt.datetime,
    until: dt.datetime,
    kind: str | None,
    page_size: int,
    limit: int | None,
) -> Iterable[dict[str, object]]:
    last_id = 0
    emitted = 0
    while True:
        page_limit = page_size
        if limit is not None:
            remaining = limit - emitted
            if remaining <= 0:
                return
            page_limit = min(page_limit, remaining)

        where = "ts >= ? AND ts < ? AND id > ?"
        params: list[object] = [_sqlite_ts(since), _sqlite_ts(until), last_id]
        if kind:
            where += " AND kind = ?"
            params.append(kind)
        params.append(page_limit)

        try:
            page = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM deploy_audit "
                f"WHERE {where} ORDER BY id ASC LIMIT ?",
                params,
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "timeout" in str(exc).lower():
                raise AuditQueryTimeout(
                    "AuditQueryTimeout: query page timed out; rerun with "
                    "a smaller --page-size or wait for the writer"
                ) from exc
            raise

        if not page:
            return
        for row in page:
            last_id = int(row["id"])
            emitted += 1
            yield {column: row[column] for column in COLUMNS}
        if len(page) < page_limit:
            return


def _print_table(rows: list[dict[str, object]]) -> None:
    headers = ("id", "ts", "kind", "tag", "actor", "status", "reason")
    print(" | ".join(headers))
    print(" | ".join("---" for _ in headers))
    for row in rows:
        values = [str(row.get(header) or "") for header in headers]
        print(" | ".join(value.replace("\n", " ") for value in values))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--since", required=True, type=_parse_since)
    parser.add_argument("--until", type=_parse_until)
    parser.add_argument("--kind", choices=sorted(KINDS))
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--format", choices=("table", "json", "csv"), default="table")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.page_size < 1:
        print("error: --page-size must be >= 1", file=sys.stderr)
        return 2
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"error: {db_path} not found", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = list(
            _rows(
                conn,
                since=args.since,
                until=args.until or dt.datetime.now(dt.timezone.utc),
                kind=args.kind,
                page_size=args.page_size,
                limit=args.limit,
            )
        )
    except AuditQueryTimeout as exc:
        print(str(exc), file=sys.stderr)
        return 3
    finally:
        conn.close()

    if args.format == "json":
        print(json.dumps(rows, indent=2, sort_keys=True, default=str))
    elif args.format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
