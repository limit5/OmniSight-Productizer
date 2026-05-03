"""KS.1.13 -- Tier 1 envelope encryption acceptance tests."""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import secret_store
from backend.security import envelope
from backend.security import kms_adapters as kms
from backend.security import secret_filter
from backend.security import spend_anomaly as sa
from backend.security import token_vault as tv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKUP_DLP_SCAN_PATH = PROJECT_ROOT / "scripts" / "backup_dlp_scan.py"

spec = importlib.util.spec_from_file_location("ks113_backup_dlp_scan", BACKUP_DLP_SCAN_PATH)
assert spec and spec.loader
backup_dlp_scan = importlib.util.module_from_spec(spec)
sys.modules["ks113_backup_dlp_scan"] = backup_dlp_scan
spec.loader.exec_module(backup_dlp_scan)


class _WrongKEKAdapter:
    provider = "fake-kms"

    def wrap_dek(self, plaintext_dek, *, encryption_context=None):
        return kms.WrappedDEK(
            provider=self.provider,
            key_id="fake-kek-v1",
            ciphertext=b"wrapped:" + plaintext_dek,
            key_version="v1",
            algorithm="fake-wrap",
            encryption_context=dict(encryption_context or {}),
        )

    def unwrap_dek(self, wrapped_dek, *, encryption_context=None):
        del wrapped_dek, encryption_context
        return b"\x00" * envelope.DEK_RAW_BYTES


def _write_backup_db(path: Path, body: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE backup_notes ("
            "id INTEGER PRIMARY KEY, "
            "body TEXT NOT NULL, "
            "encrypted_value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO backup_notes (body, encrypted_value) "
            "VALUES (:body, '')",
            {"body": body},
        )
        conn.commit()
    finally:
        conn.close()


def test_master_kek_compromise_simulation_does_not_return_plaintext() -> None:
    adapter = _WrongKEKAdapter()
    ciphertext, dek_ref = envelope.encrypt(
        "sk-ant-api03-tenant-secret-value",
        "tenant-ks113",
        kms_adapter=adapter,
        purpose="ks113-compromise-simulation",
    )

    with pytest.raises(envelope.CiphertextCorruptedError):
        envelope.decrypt(ciphertext, dek_ref, kms_adapter=adapter)


def test_envelope_round_trip_and_kek_rotation_lazy_reencrypt() -> None:
    plaintext = "ghp_AbCdEf1234567890qrstuvwxyzABCDEF12"
    first_day_v2 = (
        tv.KEY_VERSION_ROTATION_STARTED_ON
        + tv._dt.timedelta(days=tv.KEY_VERSION_ROTATION_INTERVAL_DAYS)
    )
    old = tv.encrypt_for_user(
        "user-ks113",
        "github",
        plaintext,
        tenant_id="tenant-ks113",
        as_of=tv.KEY_VERSION_ROTATION_STARTED_ON,
    )

    result = tv.decrypt_for_user_with_lazy_reencrypt(
        "user-ks113",
        "github",
        old,
        tenant_id="tenant-ks113",
        as_of=first_day_v2,
    )

    assert result.plaintext == plaintext
    assert result.key_version == 1
    assert result.target_key_version == 2
    assert result.replacement is not None
    assert result.replacement.key_version == 2
    assert tv.decrypt_for_user(
        "user-ks113",
        "github",
        result.replacement,
        as_of=first_day_v2,
    ) == plaintext


def test_dual_read_write_survives_hard_restart(monkeypatch) -> None:
    monkeypatch.setenv("OMNISIGHT_SECRET_KEY", "ks113-hard-restart-secret")
    monkeypatch.delenv(envelope.ENVELOPE_ENABLED_ENV, raising=False)
    secret_store._reset_for_tests()
    envelope_token = tv.encrypt_for_user(
        "user-ks113-restart",
        "google",
        "ya29.envelope-token",
        tenant_id="tenant-ks113-restart",
    )
    monkeypatch.setenv(envelope.ENVELOPE_ENABLED_ENV, "false")
    legacy_token = tv.encrypt_for_user(
        "user-ks113-restart",
        "google",
        "ya29.legacy-token",
        tenant_id="tenant-ks113-restart",
    )
    payload = json.dumps(
        {
            "envelope": envelope_token.__dict__,
            "legacy": legacy_token.__dict__,
        }
    )
    code = """
import json
import sys
from backend.security import token_vault as tv

raw = json.loads(sys.stdin.read())
out = []
for name in ("envelope", "legacy"):
    token = tv.EncryptedToken(**raw[name])
    out.append(tv.decrypt_for_user("user-ks113-restart", "google", token))
print("\\n".join(out))
"""

    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=payload,
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout.splitlines() == ["ya29.envelope-token", "ya29.legacy-token"]


@pytest.mark.asyncio
async def test_each_decrypt_emits_n10_aligned_audit_row(monkeypatch) -> None:
    captured = []

    async def fake_log(**kwargs):
        captured.append(kwargs)
        return len(captured)

    monkeypatch.setattr("backend.security.decryption_audit.audit.log", fake_log)
    token_a = tv.encrypt_for_user("user-audit-a", "github", "ghp_audit_a")
    token_b = tv.encrypt_for_user("user-audit-b", "github", "ghp_audit_b")

    assert await tv.decrypt_for_user_with_audit(
        "user-audit-a",
        "github",
        token_a,
        request_id="req-audit-a",
    ) == "ghp_audit_a"
    assert await tv.decrypt_for_user_with_audit(
        "user-audit-b",
        "github",
        token_b,
        request_id="req-audit-b",
    ) == "ghp_audit_b"

    assert [row["action"] for row in captured] == ["ks.decryption", "ks.decryption"]
    assert [row["entity_kind"] for row in captured] == ["decryption", "decryption"]
    assert captured[0]["before"]["request_id"] == "req-audit-a"
    assert captured[0]["after"]["user_id"] == "user-audit-a"
    assert captured[1]["before"]["request_id"] == "req-audit-b"
    assert captured[1]["after"]["user_id"] == "user-audit-b"
    assert all(row["after"]["key_id"] == "local-fernet" for row in captured)


@pytest.mark.asyncio
async def test_anomaly_alert_fires_inside_60_second_window() -> None:
    alerts = []

    async def sink(alert: sa.SpendAnomalyAlert) -> None:
        alerts.append(alert)

    detector = sa.SpendAnomalyDetector(
        store=sa.InMemorySpendAnomalyStore(),
        alert_sink=sink,
    )
    await detector.configure_threshold(
        "tenant-ks113-anomaly",
        token_rate_limit=500,
        window_seconds=60,
        throttle_seconds=120,
    )
    now = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)

    first = await detector.record_and_check(
        sa.TokenUsageEvent(
            tenant_id="tenant-ks113-anomaly",
            input_tokens=250,
            output_tokens=100,
            request_id="req-anomaly-1",
        ),
        now=now,
    )
    second = await detector.record_and_check(
        sa.TokenUsageEvent(
            tenant_id="tenant-ks113-anomaly",
            input_tokens=200,
            output_tokens=50,
            request_id="req-anomaly-2",
        ),
        now=now.replace(second=59),
    )

    assert first.allowed
    assert not second.allowed
    assert second.observed_tokens == 600
    assert second.alert is not None
    assert (second.alert.fired_at - now).total_seconds() == 59
    assert alerts == [second.alert]


def test_log_scrubber_sink_receives_redacted_value() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("backend.tests.ks113.log_scrubber")
    old_handlers = list(logger.handlers)
    old_propagate = logger.propagate
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        secret_filter.install_logging_filter(logger)
        logger.info("provider_key=%s", "sk-abcdefghijklmnopqrstuvwxyz123456")
    finally:
        logger.handlers = old_handlers
        logger.propagate = old_propagate

    sink_value = stream.getvalue()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in sink_value
    assert "provider_key=[REDACTED]" in sink_value


def test_backup_dlp_blocks_plaintext_secret_before_export(tmp_path: Path) -> None:
    db_path = tmp_path / "ks113-leaky-backup.db"
    _write_backup_db(db_path, "OpenAI key sk-abcdefghijklmnopqrstuvwxyz123456")

    report = backup_dlp_scan.scan_backup_db(db_path)

    assert report.passed is False
    assert report.total_findings == 1
    assert report.findings[0].table == "backup_notes"
    assert report.findings[0].column == "body"
    assert report.findings[0].labels == ["openai"]
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(report.to_dict())
