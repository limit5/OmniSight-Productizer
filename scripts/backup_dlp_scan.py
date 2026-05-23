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


# OP-1640 — when the gate first scanned the LIVE prod PG (not the stale SQLite
# it scanned before), `high_entropy_token` fired on every by-design opaque
# identifier — sha256 IDs, git SHAs, lookup hashes, audit/metadata JSON
# (4348 reviewed false positives, zero real leaks). High entropy is NOT a
# secret signal, so it is NOT a blocking backup-DLP label; the hard gate is
# `required_envelope_plaintext` + sensitive-named columns + the strong,
# format-specific secret labels from ``redact()``.
NON_BLOCKING_DLP_LABELS: set[str] = {"high_entropy_token"}

# Reviewed (2026-05-23, against live prod) strong-label FALSE POSITIVES — each
# is a (table, column, label) tuple confirmed benign: audit_log.actor values
# are actor identity labels like "apikey:gerrit-webhook" (not a key);
# workflow_runs.metadata keys are user/source/test_run/target_platform;
# llm_credentials.metadata holds only base_url (the real secret is the SKIPPED
# `encrypted_value` column). Scoped narrowly to (table,column,label) so the
# label still blocks on every OTHER column.
EXPECTED_DLP_LABEL_COLUMNS: set[tuple[str, str, str]] = {
    ("audit_log", "actor", "api_key_assignment"),
    ("workflow_runs", "metadata", "api_key_assignment"),
    ("llm_credentials", "metadata", "ai_internal"),
}


def _blocking_labels(table: str, column: str, labels: list[str]) -> list[str]:
    """Filter redact() labels down to the ones that should BLOCK the backup:
    drops the over-broad ``high_entropy_token`` and the narrowly-reviewed
    (table,column,label) false positives. Everything else still blocks."""
    table_key = table.strip().lower()
    column_key = column.strip().lower()
    return [
        label
        for label in labels
        if label not in NON_BLOCKING_DLP_LABELS
        and (table_key, column_key, label) not in EXPECTED_DLP_LABEL_COLUMNS
    ]


@dataclass
class BackupDLPFinding:
    table: str
    column: str
    rowid: int | str  # SQLite rowid (int) or Postgres ctid (str)
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


def _classify_cell(table: str, column: str, value: Any) -> list[str] | None:
    """Shared SQLite/Postgres cell classifier. Returns DLP finding labels for
    *value*, or ``None`` if the cell is clean. Single source of truth so the
    SQLite and Postgres scanners cannot drift."""
    if not isinstance(value, str) or not value:
        return None
    _, labels = redact(value)
    if _is_required_envelope_column(table, column):
        # FX.10.7 — sessions.token MUST be KS envelope JSON. Plaintext (or
        # unrecognised JSON) fails the gate. This is the HARD gate.
        if not _looks_like_ks_envelope(value):
            return ["required_envelope_plaintext"]
        return None
    blocking = _blocking_labels(table, column, labels)
    if blocking:
        if (table, column) in EXPECTED_HIGH_ENTROPY_COLUMNS:
            # Reviewed-and-known-safe column (legacy allowlist).
            return None
        return blocking
    # No blocking label fired, but a secret-NAMED column with any plaintext
    # value still fails — value-pattern misses don't excuse a column that is
    # supposed to hold an encrypted secret.
    if _is_sensitive_plaintext_column(column):
        return ["sensitive_column_plaintext"]
    return None


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
                    labels = _classify_cell(table, column, row[column])
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


_PG_TEXT_TYPES = {"text", "character varying", "character", "json", "jsonb", "citext"}


def _sanitize_pg_url(url: str) -> str:
    """Strip credentials from a PG URL for safe display/logging."""
    import re

    return re.sub(r"://[^@/]*@", "://***@", url)


def scan_postgres_db(database_url: str) -> BackupDLPReport:
    """Scan a PostgreSQL database for plaintext secret-shaped values.

    Mirrors :func:`scan_backup_db` but over a live PG connection (used to scan
    a throwaway DB restored from the pre-deploy ``pg_dump``). Uses the SAME
    :func:`_classify_cell` so SQLite and PG gates cannot drift. Fail-closed on
    any connection/query error (returns an error report → exit non-zero)."""
    safe = _sanitize_pg_url(database_url)
    try:
        import psycopg2  # lazy — only needed in PG mode
    except Exception as exc:  # pragma: no cover - import guard
        return BackupDLPReport(safe, 0, [], error=f"psycopg2 import failed: {exc}")

    # psycopg2 speaks plain libpq; drop any SQLAlchemy driver suffix.
    dsn = database_url.replace("+asyncpg", "").replace("+psycopg2", "")
    findings: list[BackupDLPFinding] = []
    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:
        return BackupDLPReport(safe, 0, [], error=f"connect failed: {exc}")
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        # Ordinary base tables in the public schema + their text-like columns.
        cur.execute(
            """
            SELECT c.table_name, c.column_name, c.data_type
            FROM information_schema.columns c
            JOIN information_schema.tables t
              ON t.table_schema = c.table_schema AND t.table_name = c.table_name
            WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'
            ORDER BY c.table_name, c.ordinal_position
            """
        )
        cols_by_table: dict[str, list[str]] = {}
        for table_name, column_name, data_type in cur.fetchall():
            if str(data_type).lower() not in _PG_TEXT_TYPES:
                continue
            if _is_skipped_column(str(column_name)):
                continue
            cols_by_table.setdefault(str(table_name), []).append(str(column_name))

        for table, columns in cols_by_table.items():
            if not columns:
                continue
            select_cols = ", ".join(_quote_ident(col) for col in columns)
            cur.execute(
                f"SELECT ctid::text AS __rowid, {select_cols} FROM {_quote_ident(table)}"
            )
            colnames = [d[0] for d in cur.description]
            for row in cur:
                rowmap = dict(zip(colnames, row))
                rowid = str(rowmap.get("__rowid"))
                for column in columns:
                    value = rowmap.get(column)
                    # JSON/JSONB columns arrive as parsed objects from psycopg2;
                    # _classify_cell only inspects str values, so re-serialise
                    # dict/list so envelope JSON is checked as text.
                    if isinstance(value, (dict, list)):
                        value = json.dumps(value)
                    labels = _classify_cell(table, column, value)
                    if labels:
                        findings.append(
                            BackupDLPFinding(
                                table=table, column=column, rowid=rowid, labels=labels
                            )
                        )
    except Exception as exc:
        return BackupDLPReport(safe, len(findings), findings, error=str(exc))
    finally:
        conn.close()

    return BackupDLPReport(safe, len(findings), findings)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan a SQLite backup or PostgreSQL DB for plaintext secret leakage.",
    )
    parser.add_argument("db_path", nargs="?", help="SQLite backup file path.")
    parser.add_argument(
        "--postgres-url",
        help="PostgreSQL URL to scan (e.g. a throwaway DB restored from pg_dump).",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.postgres_url:
        report = scan_postgres_db(args.postgres_url)
    elif args.db_path:
        report = scan_backup_db(args.db_path)
    else:
        parser.error("provide a SQLite db_path or --postgres-url")
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
