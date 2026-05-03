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
