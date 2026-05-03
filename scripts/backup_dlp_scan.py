#!/usr/bin/env python3
"""KS.1.8 backup DLP scanner for SQLite backup artefacts.

The prod backup pipeline creates a short-lived plaintext SQLite copy
before gpg encryption. This scanner inspects that copy and blocks if a
secret-shaped value appears in a non-encrypted text column. Raw secret
values are never printed; findings carry only table / column / rowid and
the redaction labels from ``backend.security.secret_filter``.

Module-global state audit: this script reads immutable column-name sets
and secret regex tables only; every worker/process derives findings from
the backup file contents and shares no mutable in-memory state.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.security.secret_filter import redact


SKIPPED_COLUMN_NAMES = {
    "hash",
    "password_hash",
    "prev_hash",
    "token_hash",
}

SKIPPED_COLUMN_PREFIXES = (
    "ciphertext",
    "encrypted_",
)

SKIPPED_COLUMN_SUFFIXES = (
    "_ciphertext",
    "_hash",
)


@dataclass
class BackupDLPFinding:
    table: str
    column: str
    rowid: int
    labels: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BackupDLPReport:
    db_path: str
    total_findings: int
    findings: list[BackupDLPFinding]
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.error and not self.findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "passed": self.passed,
            "total_findings": self.total_findings,
            "findings": [finding.to_dict() for finding in self.findings],
            "error": self.error,
        }


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _is_skipped_column(name: str) -> bool:
    key = name.strip().lower()
    return (
        key in SKIPPED_COLUMN_NAMES
        or any(key.startswith(prefix) for prefix in SKIPPED_COLUMN_PREFIXES)
        or any(key.endswith(suffix) for suffix in SKIPPED_COLUMN_SUFFIXES)
    )


def _iter_user_tables(conn: sqlite3.Connection) -> Iterable[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    for row in rows:
        yield str(row["name"])


def _text_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    cols: list[str] = []
    for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})"):
        name = str(row["name"])
        declared_type = str(row["type"] or "").upper()
        if _is_skipped_column(name):
            continue
        if declared_type and not any(
            marker in declared_type for marker in ("CHAR", "CLOB", "TEXT", "JSON")
        ):
            continue
        cols.append(name)
    return cols


def scan_backup_db(db_path: Path | str) -> BackupDLPReport:
    """Scan a SQLite backup for plaintext secret-shaped values."""
    path = Path(db_path).resolve()
    if not path.exists():
        return BackupDLPReport(str(path), 0, [], error=f"{path} not found")

    findings: list[BackupDLPFinding] = []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        return BackupDLPReport(str(path), 0, [], error=f"open failed: {exc}")

    try:
        conn.execute("PRAGMA query_only = ON")
        for table in _iter_user_tables(conn):
            columns = _text_columns(conn, table)
            if not columns:
                continue
            select_cols = ", ".join(_quote_ident(col) for col in columns)
            sql = f"SELECT rowid AS __rowid, {select_cols} FROM {_quote_ident(table)}"
            for row in conn.execute(sql):
                rowid = int(row["__rowid"])
                for column in columns:
                    value = row[column]
                    if not isinstance(value, str) or not value:
                        continue
                    _, labels = redact(value)
                    if labels:
                        findings.append(
                            BackupDLPFinding(
                                table=table,
                                column=column,
                                rowid=rowid,
                                labels=labels,
                            )
                        )
    except sqlite3.Error as exc:
        return BackupDLPReport(str(path), len(findings), findings, error=str(exc))
    finally:
        conn.close()

    return BackupDLPReport(str(path), len(findings), findings)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a SQLite backup for plaintext secret leakage.",
    )
    parser.add_argument("db_path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = scan_backup_db(args.db_path)
    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    elif report.passed:
        print(f"backup DLP passed: {report.db_path}")
    elif report.error:
        print(f"backup DLP error: {report.error}", file=sys.stderr)
    else:
        print(
            f"backup DLP blocked {report.total_findings} plaintext secret finding(s)",
            file=sys.stderr,
        )
        for finding in report.findings:
            labels = ",".join(finding.labels)
            print(
                f"  {finding.table}.{finding.column} rowid={finding.rowid} "
                f"labels={labels}",
                file=sys.stderr,
            )
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
