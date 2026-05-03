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
    assert "python3 scripts/backup_dlp_scan.py \"$PLAIN\"" in text
    assert "backup DLP scan failed; plaintext backup shredded" in text
    assert "OMNISIGHT_BACKUP_PASSPHRASE unset" not in text
