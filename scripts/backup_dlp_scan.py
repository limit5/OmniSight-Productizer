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
    "_enc",
    "_fingerprint",
    "_hash",
    "_ref",
)

SENSITIVE_COLUMN_MARKERS = (
    "api_key",
    "access_token",
    "client_secret",
    "credential",
    "password_plaintext",
    "private_key",
    "refresh_token",
    "secret_value",
    "webhook_secret",
)

REQUIRED_ENVELOPE_COLUMNS: set[tuple[str, str]] = {
    # FX.11.3 — sessions.token MUST be KS envelope JSON post-FX.11.1
    # backfill (alembic 0189) + FX.11.2 envelope-aware writer
    # (backend/auth.py:851). Plaintext rows here fail the gate.
    ("sessions", "token"),
}


# Known intentional high-entropy columns. The redact() classifier flags
# these as ``high_entropy_token`` because they are opaque IDs / SHA
# hashes / by-design session tokens — not unintended secret leaks. The
# DLP gate is meant to catch ACCIDENTAL plaintext secrets that snuck
# into a column not yet migrated to KS.1 envelope encryption; columns
# in this allowlist are reviewed and known-safe.
EXPECTED_HIGH_ENTROPY_COLUMNS: set[tuple[str, str]] = {
    ("audit_log", "session_id"),                  # opaque session reference, not the token
    ("prompt_versions", "body_sha256"),           # SHA-256 hash, by definition high entropy
    ("sessions", "token_lookup_index"),           # FX.11.1 added column: sha256(plaintext_token) hex
}


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


def _is_sensitive_plaintext_column(name: str) -> bool:
    key = name.strip().lower()
    return any(marker in key for marker in SENSITIVE_COLUMN_MARKERS)


def _is_required_envelope_column(table: str, column: str) -> bool:
    return (table.strip().lower(), column.strip().lower()) in REQUIRED_ENVELOPE_COLUMNS


def _looks_like_ks_envelope(value: str) -> bool:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    if {"ciphertext", "dek_ref"}.issubset(payload):
        dek_ref = payload.get("dek_ref")
        return isinstance(payload.get("ciphertext"), str) and isinstance(dek_ref, dict)
    return {"dek", "tid", "nonce_b64", "ciphertext_b64"}.issubset(payload)


def _iter_user_tables(conn: sqlite3.Connection) -> Iterable[str]:
    # Skip virtual tables (FTS5 / RTree / etc) and WITHOUT ROWID tables —
    # neither exposes the implicit ``rowid`` column the DLP scanner uses
    # to anchor findings; DDL inspection via ``sqlite_master.sql`` is the
    # only reliable filter (PRAGMA table_info won't tell us "WITHOUT ROWID").
    # FTS5 shadow tables (``*_fts``, ``*_fts_config``, ``*_fts_idx``,
    # ``*_fts_data``, ``*_fts_docsize``) hold tokenised search index data
    # only — never the original plaintext, which lives in the source table
    # the DLP scan already covers — so dropping them creates no DLP gap.
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    for row in rows:
        sql = (row["sql"] or "").upper()
        if "CREATE VIRTUAL" in sql or "WITHOUT ROWID" in sql:
            continue
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
                    if _is_required_envelope_column(table, column):
                        # FX.10.7 — sessions.token MUST be KS envelope JSON.
                        # Plaintext (or unrecognised JSON) fails the gate.
                        if not _looks_like_ks_envelope(value):
                            findings.append(
                                BackupDLPFinding(
                                    table=table,
                                    column=column,
                                    rowid=rowid,
                                    labels=["required_envelope_plaintext"],
                                )
                            )
                    elif labels:
                        if (table, column) in EXPECTED_HIGH_ENTROPY_COLUMNS:
                            # Reviewed-and-known-safe column; DLP gate is for
                            # unintentional plaintext leaks elsewhere.
                            continue
                        findings.append(
                            BackupDLPFinding(
                                table=table,
                                column=column,
                                rowid=rowid,
                                labels=labels,
                            )
                        )
                    elif _is_sensitive_plaintext_column(column):
                        findings.append(
                            BackupDLPFinding(
                                table=table,
                                column=column,
                                rowid=rowid,
                                labels=["sensitive_column_plaintext"],
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
