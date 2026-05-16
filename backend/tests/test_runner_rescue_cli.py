"""OP-1118 v2-Ⅹ-RescueCLI — tests for scripts/omnisight-rescue/runner_rescue.py.

Covers the 3 subcommands + the operator allow-list gate + the audit-log
side effects. The CLI module is imported dynamically because it lives
under ``scripts/omnisight-rescue/`` (hyphenated dir not a package).
"""
from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from backend.agents import runner_coordination as rc


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "scripts" / "omnisight-rescue" / "runner_rescue.py"


def _load_cli():
    sys.modules.pop("runner_rescue_cli", None)
    spec = importlib.util.spec_from_file_location("runner_rescue_cli", CLI_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["runner_rescue_cli"] = mod
    spec.loader.exec_module(mod)
    return mod


_SCHEMA = """
CREATE TABLE runner_claims (
    lease_id TEXT PRIMARY KEY, ticket_key TEXT NOT NULL,
    resource_key TEXT NOT NULL, owner_agent_class TEXT NOT NULL,
    owner_instance_id TEXT NOT NULL, fencing_token TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'active', phase TEXT NOT NULL DEFAULT 'pickup',
    heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at TEXT, release_reason TEXT,
    external_refs TEXT NOT NULL DEFAULT '{}',
    CHECK (state IN ('active', 'released'))
);
CREATE UNIQUE INDEX uq_runner_claims_resource_active
    ON runner_claims (resource_key) WHERE state = 'active';
CREATE TABLE runner_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL,
    operator_fingerprint TEXT,
    target_lease_id TEXT,
    target_ticket_key TEXT,
    details TEXT NOT NULL DEFAULT '{}'
);
"""


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    p = tmp_path / "test_rescue.db"
    monkeypatch.setenv("OMNISIGHT_DATABASE_PATH", str(p))
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return p


@pytest.fixture()
def cli():
    return _load_cli()


def _audit_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT action, operator_fingerprint, target_lease_id, "
        "target_ticket_key, details FROM runner_audit_events ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── operator allowlist gate ───────────────────────────────────────────


def test_operator_allowed_with_empty_env_accepts_any(cli, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_L2_OPERATORS", raising=False)
    assert cli._operator_allowed("anyone") is True
    assert cli._operator_allowed("") is False  # empty fingerprint still rejected


def test_operator_allowed_respects_allowlist(cli, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_L2_OPERATORS", "alice, bob,carol")
    assert cli._operator_allowed("alice") is True
    assert cli._operator_allowed("bob") is True
    assert cli._operator_allowed("carol") is True
    assert cli._operator_allowed("dave") is False
    assert cli._operator_allowed("") is False


# ── dump subcommand ───────────────────────────────────────────────────


def test_dump_empty_state(cli, db_path, capsys):
    rc_code = cli.main(["dump"])
    assert rc_code == 0
    out = capsys.readouterr().out
    assert "(no active claims)" in out
    # Even empty dump should still audit-log
    rows = _audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "rescue.dump"


def test_dump_table_output_shows_active_claims(cli, db_path, capsys):
    rc.acquire_claim(
        ticket_key="OP-d1", resource_key="ticket:OP-d1",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    rc.acquire_claim(
        ticket_key="OP-d2", resource_key="ticket:OP-d2",
        owner_agent_class="subscription-codex", owner_instance_id="codex-1",
    )

    rc_code = cli.main(["dump"])
    assert rc_code == 0
    out = capsys.readouterr().out
    assert "OP-d1" in out
    assert "OP-d2" in out
    assert "subscription-claude" in out
    assert "subscription-codex" in out
    assert "TICKET" in out  # header present


def test_dump_json_output_is_parseable(cli, db_path, capsys):
    rc.acquire_claim(
        ticket_key="OP-j1", resource_key="ticket:OP-j1",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    rc_code = cli.main(["dump", "--json"])
    assert rc_code == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["ticket_key"] == "OP-j1"
    assert payload[0]["owner_agent_class"] == "subscription-claude"


def test_dump_audit_logs_record_count_and_operator(cli, db_path, capsys):
    rc.acquire_claim(
        ticket_key="OP-a1", resource_key="ticket:OP-a1",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    cli.main(["dump", "--operator", "alice"])
    rows = _audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["operator_fingerprint"] == "alice"
    details = json.loads(rows[0]["details"])
    assert details["count"] == 1
    assert details["format"] == "table"


# ── release subcommand ────────────────────────────────────────────────


def test_release_requires_operator_allowlist(cli, db_path, capsys, monkeypatch):
    """When OMNISIGHT_L2_OPERATORS is set and operator not in list →
    exit code 2 (auth refusal)."""
    monkeypatch.setenv("OMNISIGHT_L2_OPERATORS", "alice,bob")
    rc_code = cli.main([
        "release", "some-lease",
        "--reason", "test",
        "--operator", "mallory",
    ])
    assert rc_code == 2
    err = capsys.readouterr().err
    assert "not in OMNISIGHT_L2_OPERATORS" in err


def test_release_forces_lease_to_released_state(cli, db_path, capsys, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_L2_OPERATORS", raising=False)
    lease = rc.acquire_claim(
        ticket_key="OP-r1", resource_key="ticket:OP-r1",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    rc_code = cli.main([
        "release", lease.lease_id,
        "--reason", "dead runner — host-reboot orphan",
        "--operator", "alice",
    ])
    assert rc_code == 0
    out = capsys.readouterr().out
    assert "Released lease" in out
    assert lease.lease_id in out

    # Lease is now released
    assert rc.find_active_holders(resource_keys=["ticket:OP-r1"]) == []

    # Audit row carries the snapshot + reason
    rows = _audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "rescue.release"
    assert rows[0]["target_lease_id"] == lease.lease_id
    assert rows[0]["target_ticket_key"] == "OP-r1"
    details = json.loads(rows[0]["details"])
    assert details["reason"] == "dead runner — host-reboot orphan"
    assert details["snapshot"]["resource_key"] == "ticket:OP-r1"


def test_release_returns_3_when_no_active_lease(cli, db_path, capsys, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_L2_OPERATORS", raising=False)
    rc_code = cli.main([
        "release", "never-existed",
        "--reason", "test",
        "--operator", "alice",
    ])
    assert rc_code == 3
    rows = _audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "rescue.release_no_match"
    assert rows[0]["target_lease_id"] == "never-existed"


def test_release_bypasses_fencing_token(cli, db_path, monkeypatch):
    """Operator override must work even without the lease's fencing
    token (the whole point — the original holder is dead and we don't
    have its token)."""
    monkeypatch.delenv("OMNISIGHT_L2_OPERATORS", raising=False)
    lease = rc.acquire_claim(
        ticket_key="OP-f1", resource_key="ticket:OP-f1",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    # The operator only knows the lease_id (from `dump` output)
    rc_code = cli.main([
        "release", lease.lease_id,
        "--reason", "fencing-token unknown — dead runner",
        "--operator", "alice",
    ])
    assert rc_code == 0
    assert rc.find_active_holders(resource_keys=["ticket:OP-f1"]) == []


# ── reset subcommand ──────────────────────────────────────────────────


def test_reset_requires_operator(cli, db_path, capsys, monkeypatch):
    monkeypatch.setenv("OMNISIGHT_L2_OPERATORS", "alice")
    rc_code = cli.main(["reset", "--operator", "evil"])
    assert rc_code == 2


def test_reset_releases_stale_claims_only(cli, db_path, capsys, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_L2_OPERATORS", raising=False)
    fresh = rc.acquire_claim(
        ticket_key="OP-rst-1", resource_key="ticket:OP-rst-1",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    stale = rc.acquire_claim(
        ticket_key="OP-rst-2", resource_key="ticket:OP-rst-2",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    # Backdate stale claim's heartbeat to 1 hour ago
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=3600)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE runner_claims SET heartbeat_at = ? WHERE lease_id = ?",
                 (old, stale.lease_id))
    conn.commit()
    conn.close()

    rc_code = cli.main([
        "reset", "--operator", "alice",
        "--max-age-seconds", "300",
    ])
    assert rc_code == 0
    out = capsys.readouterr().out
    assert "Released 1 stale lease" in out

    # Fresh kept, stale released
    active = {h.lease_id for h in rc.find_active_holders()}
    assert fresh.lease_id in active
    assert stale.lease_id not in active

    rows = _audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "rescue.reset"
    details = json.loads(rows[0]["details"])
    assert details["released_count"] == 1


def test_reset_dry_run_does_not_release_but_does_audit(cli, db_path, capsys, monkeypatch):
    monkeypatch.delenv("OMNISIGHT_L2_OPERATORS", raising=False)
    stale = rc.acquire_claim(
        ticket_key="OP-dry-1", resource_key="ticket:OP-dry-1",
        owner_agent_class="subscription-claude", owner_instance_id="claude-1",
    )
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(seconds=3600)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    conn = sqlite3.connect(str(db_path))
    conn.execute("UPDATE runner_claims SET heartbeat_at = ? WHERE lease_id = ?",
                 (old, stale.lease_id))
    conn.commit()
    conn.close()

    rc_code = cli.main([
        "reset", "--operator", "alice",
        "--max-age-seconds", "300",
        "--dry-run",
    ])
    assert rc_code == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "would release 1" in out

    # Stale still active — dry-run didn't actually release
    active = {h.lease_id for h in rc.find_active_holders()}
    assert stale.lease_id in active

    rows = _audit_rows(db_path)
    assert len(rows) == 1
    assert rows[0]["action"] == "rescue.reset_dry_run"
    details = json.loads(rows[0]["details"])
    assert details["would_release_count"] == 1
