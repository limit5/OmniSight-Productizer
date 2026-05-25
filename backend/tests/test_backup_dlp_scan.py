"""KS.1.8 — Backup pipeline DLP scanner tests."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "backup_dlp_scan.py"

spec = importlib.util.spec_from_file_location("backup_dlp_scan", SCRIPT_PATH)
assert spec and spec.loader
backup_dlp_scan = importlib.util.module_from_spec(spec)
sys.modules["backup_dlp_scan"] = backup_dlp_scan
spec.loader.exec_module(backup_dlp_scan)


def _write_db(path: Path, rows: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE audit_notes ("
            "id INTEGER PRIMARY KEY, "
            "body TEXT NOT NULL, "
            "encrypted_value TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO audit_notes (body, encrypted_value) "
            "VALUES (:body, :encrypted_value)",
            [
                {"body": body, "encrypted_value": encrypted_value}
                for body, encrypted_value in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_scan_backup_db_passes_clean_text(tmp_path: Path) -> None:
    db_path = tmp_path / "clean.db"
    _write_db(db_path, [("routine audit note", "")])

    report = backup_dlp_scan.scan_backup_db(db_path)

    assert report.passed is True
    assert report.total_findings == 0


def test_scan_backup_db_blocks_plaintext_secret(tmp_path: Path) -> None:
    db_path = tmp_path / "leaky.db"
    _write_db(db_path, [("OpenAI key sk-abcdefghijklmnopqrstuvwxyz123456", "")])

    report = backup_dlp_scan.scan_backup_db(db_path)

    assert report.passed is False
    assert report.total_findings == 1
    finding = report.findings[0]
    assert finding.table == "audit_notes"
    assert finding.column == "body"
    assert finding.rowid == 1
    assert finding.labels == ["openai"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(report.to_dict())


def test_scan_backup_db_skips_encrypted_secret_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "encrypted.db"
    _write_db(
        db_path,
        [("ciphertext is expected here", "sk-abcdefghijklmnopqrstuvwxyz123456")],
    )

    report = backup_dlp_scan.scan_backup_db(db_path)

    assert report.passed is True
    assert report.total_findings == 0


def test_scan_backup_db_skips_token_ciphertext_suffix(tmp_path: Path) -> None:
    db_path = tmp_path / "oauth.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE oauth_tokens ("
            "id INTEGER PRIMARY KEY, "
            "access_token_enc TEXT NOT NULL, "
            "refresh_token_enc TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO oauth_tokens (access_token_enc, refresh_token_enc) "
            "VALUES (:access_token_enc, :refresh_token_enc)",
            {
                "access_token_enc": "short-token-that-would-be-sensitive",
                "refresh_token_enc": "another-short-token",
            },
        )
        conn.commit()
    finally:
        conn.close()

    report = backup_dlp_scan.scan_backup_db(db_path)

    assert report.passed is True
    assert report.total_findings == 0


def test_scan_backup_db_blocks_plaintext_sensitive_column(tmp_path: Path) -> None:
    db_path = tmp_path / "plaintext-token.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE webhook_deliveries ("
            "id INTEGER PRIMARY KEY, "
            "access_token TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO webhook_deliveries (access_token) VALUES (:access_token)",
            {"access_token": "short-token-that-missed-secret-regex"},
        )
        conn.commit()
    finally:
        conn.close()

    report = backup_dlp_scan.scan_backup_db(db_path)

    assert report.passed is False
    assert report.total_findings == 1
    finding = report.findings[0]
    assert finding.table == "webhook_deliveries"
    assert finding.column == "access_token"
    assert finding.labels == ["sensitive_column_plaintext"]


def test_scan_backup_db_blocks_plaintext_session_token(tmp_path: Path) -> None:
    db_path = tmp_path / "plaintext-session.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            "token TEXT PRIMARY KEY, "
            "user_id TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sessions (token, user_id) VALUES (:token, :user_id)",
            {
                "token": "sess_" + ("A" * 48),
                "user_id": "u-session",
            },
        )
        conn.commit()
    finally:
        conn.close()

    report = backup_dlp_scan.scan_backup_db(db_path)

    assert report.passed is False
    assert report.total_findings == 1
    finding = report.findings[0]
    assert finding.table == "sessions"
    assert finding.column == "token"
    assert finding.labels == ["required_envelope_plaintext"]
    assert "sess_AAAA" not in json.dumps(report.to_dict())


def test_scan_backup_db_allows_enveloped_session_token(tmp_path: Path) -> None:
    db_path = tmp_path / "enveloped-session.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE sessions ("
            "token TEXT PRIMARY KEY, "
            "user_id TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sessions (token, user_id) VALUES (:token, :user_id)",
            {
                "token": json.dumps(
                    {
                        "fmt": 1,
                        "ciphertext": (
                            '{"fmt":1,"dek":"dek-session","tid":"t-default",'
                            '"nonce_b64":"bm9uY2U=","ciphertext_b64":"Y2lwaGVy"}'
                        ),
                        "dek_ref": {
                            "dek_id": "dek-session",
                            "tenant_id": "t-default",
                            "key_id": "local",
                            "provider": "local-fernet",
                            "wrapped_dek_b64": "d3JhcHBlZA==",
                        },
                    },
                    sort_keys=True,
                ),
                "user_id": "u-session",
            },
        )
        conn.commit()
    finally:
        conn.close()

    report = backup_dlp_scan.scan_backup_db(db_path)

    assert report.passed is True
    assert report.total_findings == 0


def test_cli_json_returns_nonzero_without_raw_secret(tmp_path: Path) -> None:
    db_path = tmp_path / "leaky.db"
    _write_db(db_path, [("token ghp_AbCdEf1234567890qrstuvwxyzABCDEF12", "")])

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(db_path),
            "--json",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["passed"] is False
    assert payload["findings"][0]["labels"] == ["github_pat"]
    assert "ghp_AbCdEf" not in proc.stdout


def test_backup_prod_db_requires_passphrase_and_dlp() -> None:
    text = (PROJECT_ROOT / "scripts" / "backup_prod_db.sh").read_text()

    assert "OMNISIGHT_BACKUP_PASSPHRASE is required" in text
    # FX.7.10 — DLP scanner is invoked through the validated $DLP_SCANNER
    # variable (not a bare relative path), so the preflight existence
    # check and the actual call share one source of truth.
    assert 'DLP_SCANNER="$REPO/scripts/backup_dlp_scan.py"' in text
    assert 'python3 "$DLP_SCANNER" "$PLAIN"' in text
    assert "backup DLP scan failed; plaintext backup shredded" in text
    assert "OMNISIGHT_BACKUP_PASSPHRASE unset" not in text


def test_backup_prod_db_uploads_immutable_s3_with_encryption() -> None:
    text = (PROJECT_ROOT / "scripts" / "backup_prod_db.sh").read_text()

    assert "OMNISIGHT_BACKUP_S3_URI" in text
    assert "aws s3api put-object" in text
    assert "--server-side-encryption AES256" in text
    assert "--server-side-encryption aws:kms" in text
    assert "--object-lock-mode COMPLIANCE" in text
    assert "--object-lock-retain-until-date" in text
    assert "--storage-class \"$storage_class\"" in text
    assert "GLACIER_IR" in text
    assert "upload_offsite_immutable \"$FINAL\"" in text


# ── OP-1640: high_entropy_token is no longer a blocking signal (it fires on
# every by-design opaque ID / SHA / hash); the hard gate stays on
# required_envelope_plaintext + sensitive-named columns + strong secret labels.
def _write_single(path: Path, table: str, column: str, value: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, {column} TEXT)")
        conn.execute(f"INSERT INTO {table} ({column}) VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def test_high_entropy_token_alone_does_not_block(tmp_path: Path) -> None:
    db_path = tmp_path / "opaque.db"
    # 64-char hex opaque id — would fire high_entropy_token; must NOT block now.
    _write_single(db_path, "runner_incidents", "incident_id", "a" * 64)
    report = backup_dlp_scan.scan_backup_db(db_path)
    assert report.passed is True, report.to_dict()


def test_sessions_token_plaintext_still_blocks(tmp_path: Path) -> None:
    db_path = tmp_path / "sess.db"
    _write_single(db_path, "sessions", "token", "plaintext_not_envelope_json")
    report = backup_dlp_scan.scan_backup_db(db_path)
    assert report.passed is False
    assert report.findings[0].labels == ["required_envelope_plaintext"]


def test_sensitive_named_column_plaintext_still_blocks(tmp_path: Path) -> None:
    db_path = tmp_path / "cred.db"
    # A non-regex-matching value in a secret-NAMED column must still block.
    _write_single(db_path, "creds", "client_secret", "just-some-opaque-value-xyz")
    report = backup_dlp_scan.scan_backup_db(db_path)
    assert report.passed is False


def test_blocking_labels_drops_high_entropy_and_reviewed_tuples() -> None:
    f = backup_dlp_scan._blocking_labels
    assert f("x", "y", ["high_entropy_token"]) == []
    assert f("audit_log", "actor", ["api_key_assignment"]) == []
    assert f("llm_credentials", "metadata", ["ai_internal"]) == []
    # the same labels still block on OTHER columns / other labels survive
    assert f("creds", "field", ["api_key_assignment"]) == ["api_key_assignment"]
    assert f("x", "y", ["high_entropy_token", "openai"]) == ["openai"]


# ── OP-1731: the throwaway-DB DLP scan DSN must keep OMNISIGHT_DATABASE_URL's
# host (pg-primary), NOT silently fall back to psycopg2's 127.0.0.1 default. ──


def test_tmp_db_url_preserves_pg_primary_host() -> None:
    """The DLP scan DSN is derived from OMNISIGHT_DATABASE_URL's host
    (pg-primary), with only the database name swapped to the throwaway DB."""
    base = "postgresql+asyncpg://omnisight:s3cr3t@pg-primary:5432/omnisight"
    url = backup_dlp_scan.build_tmp_db_url(base, "omnisight_backup_dlp_tmp")

    from urllib.parse import urlsplit

    parts = urlsplit(url)
    assert parts.hostname == "pg-primary"
    assert parts.port == 5432
    assert parts.username == "omnisight"
    assert parts.password == "s3cr3t"
    # database name swapped to the throwaway DB, host untouched
    assert parts.path == "/omnisight_backup_dlp_tmp"
    # the SQLAlchemy/asyncpg driver suffix is stripped for libpq/psycopg2
    assert parts.scheme == "postgresql"
    # the bug signature must never appear
    assert "127.0.0.1" not in url
    assert "localhost" not in url


def test_tmp_db_url_preserves_query_params() -> None:
    base = "postgresql+asyncpg://u:p@pg-primary:5432/omnisight?sslmode=require"
    url = backup_dlp_scan.build_tmp_db_url(base, "tmp_db")
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    assert parts.hostname == "pg-primary"
    assert parts.path == "/tmp_db"
    assert parts.query == "sslmode=require"


def test_tmp_db_url_rejects_hostless_url() -> None:
    """A hostless URL must hard-fail rather than silently produce a DSN that
    psycopg2 resolves to 127.0.0.1 (the OP-1731 shred-the-dump bug)."""
    import pytest

    with pytest.raises(ValueError, match="no host"):
        backup_dlp_scan.build_tmp_db_url("postgresql+asyncpg:///omnisight", "tmp_db")


def test_postgres_tmp_db_cli_derives_host_not_localhost(tmp_path: Path) -> None:
    """End-to-end CLI: --postgres-tmp-db reads OMNISIGHT_DATABASE_URL and
    connects to its host (pg-primary), NOT 127.0.0.1. There is no PG server in
    the test env, so the scan fails closed (exit 1) — and the connect error
    must name pg-primary, proving the host was derived correctly."""
    import os

    env = dict(os.environ)
    env["OMNISIGHT_DATABASE_URL"] = (
        "postgresql+asyncpg://omnisight:pw@pg-primary:5432/omnisight"
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--postgres-tmp-db",
            "omnisight_backup_dlp_tmp",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout)
    # hard fail (no live PG) — never a silent pass
    assert proc.returncode == 1
    assert payload["passed"] is False
    # the connection target was pg-primary, NOT the 127.0.0.1 default
    assert "pg-primary" in payload["error"]
    assert "127.0.0.1" not in payload["error"]
    # credentials never surface in the error report
    assert "pw" not in proc.stdout


def test_postgres_tmp_db_cli_fails_closed_without_database_url() -> None:
    """If OMNISIGHT_DATABASE_URL is missing, the scan must fail closed (exit 1),
    never pass — a missing DSN must not green-light keeping plaintext."""
    import os

    env = {k: v for k, v in os.environ.items() if k != "OMNISIGHT_DATABASE_URL"}
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--postgres-tmp-db",
            "tmp_db",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["passed"] is False
    assert "OMNISIGHT_DATABASE_URL" in payload["error"]


def test_backup_prod_db_pg_scan_uses_tmp_db_flag_not_rsplit() -> None:
    """The PG-mode scan must invoke the scanner with --postgres-tmp-db (host
    derived inside the scanner), not the old fragile host-side rsplit one-liner
    that produced a hostless 127.0.0.1 DSN (OP-1731)."""
    text = (PROJECT_ROOT / "scripts" / "backup_prod_db.sh").read_text()
    code_only = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "--postgres-tmp-db" in code_only
    # the fragile rsplit-based URL build must be gone from executable code
    assert "rsplit" not in code_only
    assert "OMNISIGHT_DLP_TMP_DB" not in code_only
