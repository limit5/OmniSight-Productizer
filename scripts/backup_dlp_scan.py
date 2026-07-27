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
import hashlib
import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
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


# OP-2729 — content-reviewed release for prose-bearing columns.
#
# `claude_memory_versions.body` holds operator engineering notes that by design
# DISCUSS this stack's own topology (`pg-primary`, `ai_cache`, throwaway loopback
# test DSNs). A content-pattern scan cannot distinguish "discusses X" from "leaks
# X" — architecture anti-pattern #11 at the data layer — so from the 2026-07-23
# leg-3 ingest onward this column blocked the nightly encrypted backup outright.
#
# Three rounds of adversarial review rejected every pattern-based fix. The error
# was always the same shape: a gate that says "release unless my patterns detect a
# credential" is only as sound as its detector is COMPLETE, and no regex set is
# complete over free-form prose (`postgresql+asyncpg://` and `redis://:pw@` fire
# only the hostname label; `secret_filter` has no password-assignment rule at all;
# `.pgpass`, JSON, YAML, `curl -u`, `-p<pw>` and line-split URIs all escape).
#
# So release is keyed on a human having reviewed the EXACT body, identified by
# sha256. Completeness stops being a requirement: any edit is a new digest and
# therefore a new review. The digest is computed over the value actually scanned —
# the row's own `body_sha256` column is never trusted.
DIGEST_REVIEWED_COLUMNS: set[tuple[str, str]] = {
    ("claude_memory_versions", "body"),
}

# Only these labels may ever be released by a content review. Strong-format secret
# labels (anthropic, github_pat, aws_secret, private_key_block, jwt, ...) block even
# on a reviewed body: a human can miss an `sk-ant-` in a 264 KB note, a regex cannot.
# This bounds the residual risk of the design, which is reviewer error.
DIGEST_RELEASABLE_LABELS: frozenset[str] = frozenset(
    # OP-2730 adds url_userinfo. Without it, widening secret_filter to see
    # credentials outside the DB scheme list would have blocked the nightly
    # encrypted backup on this column the moment it landed -- including on
    # bodies a human had ALREADY reviewed, because a reviewed digest is only
    # honoured for labels in this set. Measured before the change: 6 rows carry
    # credential-shaped URIs, 2 of them already reviewed.
    #
    # Releasable-by-digest is not the same as harmless: it means a human looked
    # at this exact body and accepted it. For these rows that review is on
    # record in OP-2730 -- every credential in them was triaged against its real
    # endpoint and found dead.
    {"pg_internal", "ai_internal", "database_url", "url_userinfo"}
)

# The allowlist lives OUTSIDE the release artefact on purpose: prod runs from a
# release-pinned checkout, so keeping reviewed digests in this file would mean
# dirtying that checkout on every review — which hard-fails `deploy-prod.sh` and
# emergency digest rollback. `backup_prod_db.sh` bind-mounts the host file in.
REVIEWED_DIGESTS_ENV = "OMNISIGHT_DLP_REVIEWED_BODIES"
DEFAULT_REVIEWED_DIGESTS_PATH = "/etc/omnisight/backup-dlp-reviewed-bodies.txt"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_reviewed_digests_cache: frozenset[str] | None = None


def reviewed_body_digests() -> frozenset[str]:
    """sha256 digests of bodies a human has reviewed and approved.

    A missing or unreadable file yields an EMPTY set, i.e. nothing is released and
    the gate keeps blocking. Absent evidence of review is never treated as review.
    """
    global _reviewed_digests_cache
    if _reviewed_digests_cache is None:
        path = Path(os.environ.get(REVIEWED_DIGESTS_ENV, DEFAULT_REVIEWED_DIGESTS_PATH))
        digests: set[str] = set()
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                token = raw.split("#", 1)[0].strip().lower()
                if _SHA256_RE.match(token):
                    digests.add(token)
        except OSError:
            digests = set()
        _reviewed_digests_cache = frozenset(digests)
    return _reviewed_digests_cache


def body_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    # sha256 of the cell body, populated only for DIGEST_REVIEWED_COLUMNS so the
    # operator can review and approve exact content. A digest of reviewed prose is
    # not a secret; raw values are still never carried.
    body_digest: str = ""
    # Review aids for the operator, computed only for DIGEST_REVIEWED_COLUMNS.
    # These NEVER gate anything (see the three rejected pattern-based designs);
    # they exist to point a reviewer at the parts of a body worth reading twice.
    review_hints: list[str] = field(default_factory=list)

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
        if (table.strip().lower(), column.strip().lower()) in DIGEST_REVIEWED_COLUMNS and all(
            label in DIGEST_RELEASABLE_LABELS for label in blocking
        ):
            # Content-reviewed release (OP-2729): this exact body was read and
            # approved by a human. Any edit changes the digest and blocks again.
            if body_digest(value) in reviewed_body_digests():
                return None
        return blocking
    # No blocking label fired, but a secret-NAMED column with any plaintext
    # value still fails — value-pattern misses don't excuse a column that is
    # supposed to hold an encrypted secret.
    if _is_sensitive_plaintext_column(column):
        return ["sensitive_column_plaintext"]
    return None


# Review aids, NOT a gate. Deliberately broader than `secret_filter` so they flag
# the shapes it provably misses: any scheme including +driver suffixes, any
# userinfo including an empty username, and password-ish assignments. A hint means
# "read this part before approving", never "block".
_HINT_CRED_URI = re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@]*:[^\s@]*@[^\s)'\"]+", re.I)
_HINT_LOOPBACK = re.compile(r"@(localhost|127\.0\.0\.1|\[?::1\]?)[:/]", re.I)
_HINT_ASSIGN = re.compile(
    r"(?:PGPASSWORD|password|passwd|pwd|passphrase|secret|token|api[_-]?key)"
    r"\s*[:=]\s*['\"`]?(\S{8,})",
    re.I,
)
_HINT_PRIVKEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def review_hints(value: str) -> list[str]:
    hints: list[str] = []
    non_loopback = [m.group(0) for m in _HINT_CRED_URI.finditer(value) if not _HINT_LOOPBACK.search(m.group(0))]
    if non_loopback:
        hints.append(f"non-loopback-credential-uri x{len(non_loopback)}")
    assigns = _HINT_ASSIGN.findall(value)
    if assigns:
        hints.append(f"password-like-assignment x{len(assigns)}")
    if _HINT_PRIVKEY.search(value):
        hints.append("PRIVATE-KEY-BLOCK")
    return hints


def _finding_digest(table: str, column: str, value: Any) -> str:
    """Digest carried on findings in content-reviewed columns, so the operator can
    approve exact content. Empty elsewhere — no reason to hash what is not reviewable."""
    if (table.strip().lower(), column.strip().lower()) not in DIGEST_REVIEWED_COLUMNS:
        return ""
    return body_digest(value) if isinstance(value, str) else ""


def _finding_hints(table: str, column: str, value: Any) -> list[str]:
    if (table.strip().lower(), column.strip().lower()) not in DIGEST_REVIEWED_COLUMNS:
        return []
    return review_hints(value) if isinstance(value, str) else []


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
                                body_digest=_finding_digest(table, column, row[column]),
                                review_hints=_finding_hints(table, column, row[column]),
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


def build_tmp_db_url(database_url: str, tmp_db: str) -> str:
    """Build the libpq connection URL for the throwaway-DB DLP scan.

    Takes the live ``OMNISIGHT_DATABASE_URL`` (the SQLAlchemy/asyncpg DSN the
    backend uses, e.g.
    ``postgresql+asyncpg://omnisight:***@pg-primary:5432/omnisight``) and
    returns the SAME connection target with ONLY the database name swapped to
    *tmp_db*, preserving scheme / user / password / host / port / query.

    OP-1731: the throwaway-DB URL used to be assembled by a host/shell
    ``rsplit('/')`` one-liner. In a develop-tip worktree / ephemeral
    ``docker compose run`` context that could yield a HOSTLESS DSN, and
    psycopg2 silently falls back to ``127.0.0.1`` — so the scan hit a
    connection-refused against localhost and the plaintext dump was shredded
    even though pg-primary was healthy. Parsing the URL properly (urlsplit)
    and refusing a hostless result keeps the scan pointed at the real host
    (pg-primary) regardless of cwd / COMPOSE_PROJECT_NAME, and turns a missing
    host into an explicit hard error rather than a misleading localhost
    connection failure.
    """
    from urllib.parse import urlsplit, urlunsplit

    # psycopg2 speaks plain libpq; drop any SQLAlchemy driver suffix.
    cleaned = database_url.replace("+asyncpg", "").replace("+psycopg2", "")
    parts = urlsplit(cleaned)
    if not parts.hostname:
        raise ValueError(
            "OMNISIGHT_DATABASE_URL has no host; refusing to build a DLP scan "
            "DSN that would default to psycopg2's 127.0.0.1 (OP-1731). Got: "
            + _sanitize_pg_url(database_url)
        )
    path = "/" + tmp_db.lstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


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
                                table=table,
                                column=column,
                                rowid=rowid,
                                labels=labels,
                                body_digest=_finding_digest(table, column, value),
                                review_hints=_finding_hints(table, column, value),
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
    parser.add_argument(
        "--postgres-tmp-db",
        help=(
            "Name of the throwaway DB (restored from pg_dump) to scan. The "
            "connection URL is derived from OMNISIGHT_DATABASE_URL with the host "
            "preserved and only the database name swapped to this value — so the "
            "scan reaches pg-primary regardless of cwd/worktree instead of "
            "defaulting to 127.0.0.1 (OP-1731)."
        ),
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--emit-reviewed-digests",
        action="store_true",
        help=(
            "Review aid (OP-2729): after scanning, print a ready-to-paste allowlist "
            "block for blocking findings in content-reviewed columns, annotated with "
            "hints. Printing is NOT approval — read each body before pasting."
        ),
    )
    args = parser.parse_args(argv)

    if args.postgres_tmp_db:
        base = os.environ.get("OMNISIGHT_DATABASE_URL", "")
        if not base:
            report = BackupDLPReport(
                "<OMNISIGHT_DATABASE_URL>",
                0,
                [],
                error="OMNISIGHT_DATABASE_URL is not set; cannot derive DLP scan DSN",
            )
        else:
            try:
                url = build_tmp_db_url(base, args.postgres_tmp_db)
            except ValueError as exc:
                report = BackupDLPReport("<OMNISIGHT_DATABASE_URL>", 0, [], error=str(exc))
            else:
                report = scan_postgres_db(url)
    elif args.postgres_url:
        report = scan_postgres_db(args.postgres_url)
    elif args.db_path:
        report = scan_backup_db(args.db_path)
    else:
        parser.error("provide a SQLite db_path, --postgres-url, or --postgres-tmp-db")
    if args.emit_reviewed_digests:
        candidates = [
            f for f in report.findings
            if f.body_digest and all(x in DIGEST_RELEASABLE_LABELS for x in f.labels)
        ]
        blocked_by_strong = [f for f in report.findings if f.body_digest and f not in candidates]
        print("# OP-2729 reviewed-body allowlist — REVIEW EACH BODY BEFORE PASTING.")
        print("# Printing a digest is not approval. A hint means read that part twice;")
        print("# no hint does not mean safe, only that these particular shapes are absent.")
        print(f"# generated from: {report.db_path}")
        seen: set[str] = set()
        for finding in candidates:
            if finding.body_digest in seen:
                continue
            seen.add(finding.body_digest)
            note = f"{finding.table}.{finding.column} rowid={finding.rowid} labels={','.join(finding.labels)}"
            if finding.review_hints:
                note += f"  HINTS: {'; '.join(finding.review_hints)}"
            print(f"{finding.body_digest}  # {note}")
        print(f"# {len(seen)} candidate digest(s); {len(blocked_by_strong)} finding(s) carry a "
              "non-releasable label and CANNOT be approved this way.")
        for finding in blocked_by_strong:
            print(f"#   NOT-RELEASABLE {finding.table}.{finding.column} rowid={finding.rowid} "
                  f"labels={','.join(finding.labels)}", file=sys.stderr)
        return 0 if report.passed else 1

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
