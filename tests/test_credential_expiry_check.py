"""OP-727 — Credential expiry tracker tests.

Pins the 30 / 7 / 1-day alert ladder from
``scripts/credential_expiry_check.py``. Acceptance criteria covered:

  AC1: expiry = today + 31d → no alert; expiry = today + 30d → alert.
  AC2: after rotation + inventory update, alert clears.
  AC3: expiry = 'never' (e.g. ed25519 SSH keys) skipped silently.

Plus boundary / regression tests for the 7-day band, 1-day band,
already-expired entries, and inventory schema validation.

stdlib + PyYAML only — no backend / DB / network.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "credential_expiry_check.py"


def _load_module():
    """Import the script as a module without polluting sys.path."""
    spec = importlib.util.spec_from_file_location(
        "credential_expiry_check", SCRIPT_PATH
    )
    assert spec and spec.loader, "spec loader missing for credential_expiry_check.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("credential_expiry_check", module)
    spec.loader.exec_module(module)
    return module


CEC = _load_module()


# ─────────────────────────────────────────────────────────────────────
# Inventory fixture builder
# ─────────────────────────────────────────────────────────────────────


def _write_inventory(path: Path, entries: list[dict]) -> Path:
    import yaml

    path.write_text(yaml.safe_dump({"credentials": entries}), encoding="utf-8")
    return path


def _entry(
    *,
    cred_id: str = "test-cred",
    cred_type: str = "api_key",
    expiry: str = "2099-01-01",
    last_rotated_at: str = "2026-01-01",
    owner: str = "@test-owner",
    runbook: str | None = None,
    secret_ref: str | None = None,
) -> dict:
    out: dict = {
        "id": cred_id,
        "type": cred_type,
        "expiry": expiry,
        "last_rotated_at": last_rotated_at,
        "owner": owner,
    }
    if runbook is not None:
        out["runbook"] = runbook
    if secret_ref is not None:
        out["secret_ref"] = secret_ref
    return out


# ─────────────────────────────────────────────────────────────────────
# AC1 — 30-day boundary
# ─────────────────────────────────────────────────────────────────────


def test_ac1_31_days_no_alert(tmp_path):
    today = date(2026, 5, 8)
    expiry = (today + timedelta(days=31)).isoformat()
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="t31", expiry=expiry)],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    assert report.alerts == []
    assert len(report.ok) == 1
    assert report.ok[0].id == "t31"
    assert report.ok[0].days_until_expiry == 31
    assert CEC.report_exit_code(report) == CEC.EXIT_OK


def test_ac1_30_days_alert_fires(tmp_path):
    """The boundary case from the ticket: today + 30d MUST alert."""
    today = date(2026, 5, 8)
    expiry = (today + timedelta(days=30)).isoformat()
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="t30", expiry=expiry)],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    assert len(report.alerts) == 1
    alert = report.alerts[0]
    assert alert.id == "t30"
    assert alert.severity == "warning"
    assert alert.days_until_expiry == 30
    assert CEC.report_exit_code(report) == CEC.EXIT_WARNING


# ─────────────────────────────────────────────────────────────────────
# 7-day band
# ─────────────────────────────────────────────────────────────────────


def test_8_days_warning_tier(tmp_path):
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="t8", expiry=(today + timedelta(days=8)).isoformat())],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    assert report.alerts[0].severity == "warning"
    assert report.alerts[0].days_until_expiry == 8


def test_7_days_urgent_tier(tmp_path):
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="t7", expiry=(today + timedelta(days=7)).isoformat())],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    assert report.alerts[0].severity == "urgent"
    assert report.alerts[0].days_until_expiry == 7
    assert CEC.report_exit_code(report) == CEC.EXIT_URGENT


# ─────────────────────────────────────────────────────────────────────
# 1-day band
# ─────────────────────────────────────────────────────────────────────


def test_2_days_urgent_tier(tmp_path):
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="t2", expiry=(today + timedelta(days=2)).isoformat())],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    assert report.alerts[0].severity == "urgent"
    assert report.alerts[0].days_until_expiry == 2


def test_1_day_critical_tier(tmp_path):
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="t1", expiry=(today + timedelta(days=1)).isoformat())],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    assert report.alerts[0].severity == "critical"
    assert report.alerts[0].days_until_expiry == 1
    assert CEC.report_exit_code(report) == CEC.EXIT_CRITICAL


def test_already_expired_critical_exit(tmp_path):
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="t-expired", expiry=(today - timedelta(days=3)).isoformat())],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    alert = report.alerts[0]
    assert alert.severity == "expired"
    assert alert.days_until_expiry == -3
    assert CEC.report_exit_code(report) == CEC.EXIT_CRITICAL
    assert "EXPIRED" in alert.message


# ─────────────────────────────────────────────────────────────────────
# AC2 — post-rotation clears
# ─────────────────────────────────────────────────────────────────────


def test_ac2_alert_clears_after_rotation(tmp_path):
    """Simulate the operator's rotation update: expiry pushed out,
    last_rotated_at bumped to today. Next run must report ok."""
    today = date(2026, 5, 8)
    inv_path = tmp_path / "credentials.yaml"

    # Pre-rotation: 5 days from expiry → urgent alert.
    _write_inventory(
        inv_path,
        [_entry(
            cred_id="rotating-cred",
            expiry=(today + timedelta(days=5)).isoformat(),
            last_rotated_at="2025-11-01",
        )],
    )
    pre = CEC.evaluate(
        CEC.load_inventory(inv_path), today=today, inventory_path=inv_path
    )
    assert pre.alerts and pre.alerts[0].severity == "urgent"

    # Operator rotates: new 90-day expiry, last_rotated_at = today.
    _write_inventory(
        inv_path,
        [_entry(
            cred_id="rotating-cred",
            expiry=(today + timedelta(days=90)).isoformat(),
            last_rotated_at=today.isoformat(),
        )],
    )
    post = CEC.evaluate(
        CEC.load_inventory(inv_path), today=today, inventory_path=inv_path
    )
    assert post.alerts == []
    assert len(post.ok) == 1
    assert post.ok[0].days_until_expiry == 90
    assert CEC.report_exit_code(post) == CEC.EXIT_OK


# ─────────────────────────────────────────────────────────────────────
# AC3 — expiry='never' skipped silently
# ─────────────────────────────────────────────────────────────────────


def test_ac3_never_expiry_skipped_silently(tmp_path):
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="ed25519-key", cred_type="ssh_key", expiry="never")],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    assert report.alerts == []
    assert len(report.skipped) == 1
    assert report.skipped[0].id == "ed25519-key"
    assert report.skipped[0].severity == "skipped"
    assert report.skipped[0].days_until_expiry is None
    assert CEC.report_exit_code(report) == CEC.EXIT_OK


def test_ac3_never_silent_in_text_output(tmp_path):
    """'Skipped silently' = no ALERT line, no severity tag in alerts list.
    The report still notes the count for operator visibility, which is
    the right tradeoff: silent in the alert channel, visible on demand."""
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="ed-key", cred_type="ssh_key", expiry="never")],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    text = CEC.render_text(report)
    assert "ALERTS: none" in text
    assert "ALERTS (" not in text
    assert "[WARNING]" not in text
    assert "[URGENT]" not in text
    assert "[CRITICAL]" not in text


# ─────────────────────────────────────────────────────────────────────
# Mixed inventory — exit code is the MAX severity
# ─────────────────────────────────────────────────────────────────────


def test_mixed_inventory_max_severity_wins(tmp_path):
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [
            _entry(cred_id="ok-1", expiry=(today + timedelta(days=365)).isoformat()),
            _entry(cred_id="warn-1", expiry=(today + timedelta(days=20)).isoformat()),
            _entry(cred_id="urgent-1", expiry=(today + timedelta(days=5)).isoformat()),
            _entry(cred_id="critical-1", expiry=(today + timedelta(days=1)).isoformat()),
            _entry(cred_id="never-1", cred_type="ssh_key", expiry="never"),
        ],
    )
    creds = CEC.load_inventory(inv)
    report = CEC.evaluate(creds, today=today, inventory_path=inv)
    assert {a.id for a in report.alerts} == {"warn-1", "urgent-1", "critical-1"}
    assert {a.id for a in report.skipped} == {"never-1"}
    assert {a.id for a in report.ok} == {"ok-1"}
    assert CEC.report_exit_code(report) == CEC.EXIT_CRITICAL


# ─────────────────────────────────────────────────────────────────────
# Inventory schema validation
# ─────────────────────────────────────────────────────────────────────


def test_invalid_type_rejected(tmp_path):
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="bad", cred_type="oauth_refresh_token")],
    )
    with pytest.raises(ValueError, match="unknown type"):
        CEC.load_inventory(inv)


def test_missing_required_field_rejected(tmp_path):
    raw = dedent(
        """\
        credentials:
          - id: missing-owner
            type: api_key
            expiry: 2026-12-31
            last_rotated_at: 2026-01-01
        """
    )
    inv = tmp_path / "credentials.yaml"
    inv.write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="missing required field 'owner'"):
        CEC.load_inventory(inv)


def test_malformed_date_rejected(tmp_path):
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="bad-date", expiry="2026/12/31")],
    )
    with pytest.raises(ValueError, match="expected ISO date"):
        CEC.load_inventory(inv)


def test_duplicate_id_rejected(tmp_path):
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [_entry(cred_id="dup"), _entry(cred_id="dup")],
    )
    with pytest.raises(ValueError, match="duplicate credential id"):
        CEC.load_inventory(inv)


def test_missing_inventory_file_returns_inventory_bad_exit(tmp_path):
    rc = CEC.main([
        "--inventory",
        str(tmp_path / "nonexistent.yaml"),
        "--today",
        "2026-05-08",
    ])
    assert rc == CEC.EXIT_INVENTORY_BAD


def test_validate_flag_exits_zero_for_clean_example():
    """The committed example MUST be valid — guards against schema drift
    in `configs/credentials.example.yaml` itself."""
    rc = CEC.main([
        "--inventory",
        str(REPO_ROOT / "configs" / "credentials.example.yaml"),
        "--validate",
    ])
    assert rc == CEC.EXIT_OK


# ─────────────────────────────────────────────────────────────────────
# CLI integration
# ─────────────────────────────────────────────────────────────────────


def test_cli_json_output_is_parseable(tmp_path, capsys):
    today = date(2026, 5, 8)
    inv = _write_inventory(
        tmp_path / "credentials.yaml",
        [
            _entry(cred_id="t30", expiry=(today + timedelta(days=30)).isoformat()),
            _entry(cred_id="never-1", cred_type="ssh_key", expiry="never"),
        ],
    )
    rc = CEC.main([
        "--inventory", str(inv),
        "--today", today.isoformat(),
        "--format", "json",
    ])
    assert rc == CEC.EXIT_WARNING
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["today"] == today.isoformat()
    assert len(payload["alerts"]) == 1
    assert payload["alerts"][0]["id"] == "t30"
    assert payload["alerts"][0]["severity"] == "warning"
    assert payload["alerts"][0]["days_until_expiry"] == 30
    assert len(payload["skipped"]) == 1
    assert payload["skipped"][0]["id"] == "never-1"


def test_cli_today_override_must_be_iso(tmp_path):
    inv = _write_inventory(tmp_path / "credentials.yaml", [_entry()])
    rc = CEC.main(["--inventory", str(inv), "--today", "yesterday"])
    assert rc == CEC.EXIT_USAGE
