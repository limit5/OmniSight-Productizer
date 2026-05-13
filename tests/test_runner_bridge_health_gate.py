"""SP-B-X-009 (OP-1067) — C9 bridge-health pickup gate.

Three layers of coverage:

* Stale-check unit tests with a mocked file mtime — the AC's primary
  contract is the ``check_bridge_heartbeat`` helper used by the runner
  pickup gate.
* Edge: heartbeat file missing entirely → treated as stale.
* Integration: ``_bridge_health_pickup_gate`` blocks pickup *and*
  invokes ``operator_notifier.notify`` with ``Severity.CRITICAL`` when
  the bridge daemon is unresponsive.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.agents import gerrit_jira_bridge as bridge  # noqa: E402
from backend.agents.operator_notifier import Severity  # noqa: E402


def _load_runner():
    path = REPO_ROOT / "auto-runner-jira.py"
    spec = importlib.util.spec_from_file_location("auto_runner_jira", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── helpers ────────────────────────────────────────────────────────────


class _FakeClient:
    """Stand-in DispatchClient — the gate never touches its fields when
    the canary env var is unset, so we only need an opaque object."""

    bot_email = "rt3628+claude-bot@gmail.com"
    bot_account_id = "acct-1"


# ── unit: check_bridge_heartbeat ───────────────────────────────────────


def test_check_bridge_heartbeat_fresh_file_is_ok(tmp_path):
    hb = tmp_path / "heartbeat"
    hb.touch()
    # Mock: file mtime ten seconds ago, threshold 300 ⇒ fresh.
    import os
    os.utime(hb, (1_700_000_000.0, 1_700_000_000.0))

    is_fresh, age, resolved = bridge.check_bridge_heartbeat(
        path=hb, stale_after_seconds=300, now=1_700_000_010.0,
    )
    assert is_fresh is True
    assert age == pytest.approx(10.0)
    assert resolved == hb


def test_check_bridge_heartbeat_stale_file_is_blocked(tmp_path):
    hb = tmp_path / "heartbeat"
    hb.touch()
    import os
    os.utime(hb, (1_700_000_000.0, 1_700_000_000.0))

    is_fresh, age, resolved = bridge.check_bridge_heartbeat(
        path=hb, stale_after_seconds=300, now=1_700_001_000.0,
    )
    assert is_fresh is False
    assert age == pytest.approx(1_000.0)
    assert resolved == hb


def test_check_bridge_heartbeat_missing_file_treated_as_stale(tmp_path):
    """Edge: heartbeat file missing entirely → treat as stale."""

    missing = tmp_path / "never-written"
    is_fresh, age, resolved = bridge.check_bridge_heartbeat(
        path=missing, stale_after_seconds=300, now=1_700_000_000.0,
    )
    assert is_fresh is False
    assert age == float("inf")
    assert resolved == missing


def test_touch_heartbeat_file_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "heartbeat"
    assert not target.parent.exists()
    bridge.touch_heartbeat_file(target, now=1_700_000_000.0)
    assert target.exists()
    assert target.stat().st_mtime == pytest.approx(1_700_000_000.0)


def test_heartbeat_stale_after_seconds_env_override():
    assert bridge.heartbeat_stale_after_seconds(env={}) == 300
    assert bridge.heartbeat_stale_after_seconds(
        env={"OMNISIGHT_BRIDGE_STALE_AFTER_SEC": "120"}
    ) == 120
    # Invalid → falls back to default rather than crashing the gate.
    assert bridge.heartbeat_stale_after_seconds(
        env={"OMNISIGHT_BRIDGE_STALE_AFTER_SEC": "not-a-number"}
    ) == 300


def test_heartbeat_path_env_override(tmp_path):
    override = tmp_path / "alt-heartbeat"
    resolved = bridge.heartbeat_path_from_env(
        env={"OMNISIGHT_BRIDGE_HEARTBEAT_PATH": str(override)}
    )
    assert resolved == override


# ── integration: pickup gate calls notify(CRITICAL) on stale ───────────


def test_pickup_gate_blocks_pickup_and_notifies_critical(monkeypatch, tmp_path):
    runner = _load_runner()
    notify_calls: list[dict[str, Any]] = []

    def fake_check():
        return False, 9999.0, tmp_path / "heartbeat"

    def fake_notify(severity, code, message, context=None, **kw):
        notify_calls.append({
            "severity": severity,
            "code": code,
            "message": message,
            "context": context,
            **kw,
        })

    comment_calls: list[Any] = []

    monkeypatch.delenv("OMNISIGHT_FLEET_HEALTH_CANARY_KEY", raising=False)
    monkeypatch.setattr(runner, "DRY_RUN", False)

    ok = runner._bridge_health_pickup_gate(
        _FakeClient(),
        check_fn=fake_check,
        notify_fn=fake_notify,
        comment_fn=lambda *a, **kw: comment_calls.append((a, kw)),
    )

    assert ok is False
    assert len(notify_calls) == 1
    call = notify_calls[0]
    assert call["severity"] == Severity.CRITICAL
    assert call["code"] == "bridge_down"
    assert "bridge heartbeat stale" in call["message"]
    assert call["context"]["age_sec"] == 9999.0
    assert "heartbeat_path" in call["context"]
    assert comment_calls == [], "JIRA comment must be suppressed without canary key"


def test_pickup_gate_passes_when_heartbeat_fresh(monkeypatch, tmp_path):
    runner = _load_runner()
    notify_calls: list[Any] = []

    def fake_check():
        return True, 5.0, tmp_path / "heartbeat"

    monkeypatch.setattr(runner, "DRY_RUN", False)
    ok = runner._bridge_health_pickup_gate(
        _FakeClient(),
        check_fn=fake_check,
        notify_fn=lambda *a, **kw: notify_calls.append((a, kw)),
        comment_fn=lambda *a, **kw: None,
    )

    assert ok is True
    assert notify_calls == [], "fresh heartbeat must not invoke notifier"


def test_pickup_gate_canary_key_posts_jira_comment(monkeypatch, tmp_path):
    runner = _load_runner()
    comment_calls: list[tuple[Any, ...]] = []

    def fake_check():
        return False, float("inf"), tmp_path / "heartbeat"

    monkeypatch.setattr(runner, "DRY_RUN", False)
    monkeypatch.setenv("OMNISIGHT_FLEET_HEALTH_CANARY_KEY", "OP-1234")

    ok = runner._bridge_health_pickup_gate(
        _FakeClient(),
        check_fn=fake_check,
        notify_fn=lambda *a, **kw: None,
        comment_fn=lambda *args, **kw: comment_calls.append(args),
    )

    assert ok is False
    assert len(comment_calls) == 1
    # comment_fn signature: (client, key, body)
    _, key, body = comment_calls[0]
    assert key == "OP-1234"
    assert "[runner-bridge-down]" in body
    assert "bridge-health-degraded" in body


def test_pickup_gate_canary_key_skipped_in_dry_run(monkeypatch, tmp_path):
    runner = _load_runner()
    comment_calls: list[Any] = []

    def fake_check():
        return False, float("inf"), tmp_path / "heartbeat"

    monkeypatch.setattr(runner, "DRY_RUN", True)
    monkeypatch.setenv("OMNISIGHT_FLEET_HEALTH_CANARY_KEY", "OP-1234")

    ok = runner._bridge_health_pickup_gate(
        _FakeClient(),
        check_fn=fake_check,
        notify_fn=lambda *a, **kw: None,
        comment_fn=lambda *a, **kw: comment_calls.append((a, kw)),
    )

    assert ok is False
    assert comment_calls == [], "DRY_RUN must suppress JIRA writes"


def test_pickup_gate_swallows_notifier_failure(monkeypatch, tmp_path, capsys):
    """A flaky notifier must not strand the gate — return value still
    reflects the bridge being down (False) so the runner skips."""

    runner = _load_runner()

    def fake_check():
        return False, 1234.0, tmp_path / "heartbeat"

    def broken_notify(*a, **kw):
        raise RuntimeError("notifier offline")

    monkeypatch.setattr(runner, "DRY_RUN", False)
    monkeypatch.delenv("OMNISIGHT_FLEET_HEALTH_CANARY_KEY", raising=False)

    ok = runner._bridge_health_pickup_gate(
        _FakeClient(),
        check_fn=fake_check,
        notify_fn=broken_notify,
        comment_fn=lambda *a, **kw: None,
    )
    assert ok is False
    captured = capsys.readouterr()
    assert "bridge_down notify failed" in captured.err


# ── integration: bridge daemon writes the heartbeat file ───────────────


def test_bridge_maintenance_tick_writes_heartbeat_file(tmp_path, monkeypatch):
    """Spinning the bridge's maintenance tick must produce a fresh
    heartbeat the runner gate can consume on the next pickup."""

    heartbeat = tmp_path / "heartbeat"
    from backend.agents import jira_dispatch

    client = jira_dispatch.DispatchClient(
        agent_class="subscription-claude",
        base_url="https://example.atlassian.net/rest/api/3",
        project_key="OP",
        auth_header="Basic test",
        bot_account_id="acct-1",
        bot_email="rt3628+claude-bot@gmail.com",
    )

    daemon = bridge.GerritJiraBridge(
        client,
        bridge.BridgeConfig(
            heartbeat_seconds=9999,
            heartbeat_file_seconds=0.0,  # tick every call → deterministic test
            heartbeat_file_path=heartbeat,
            periodic_catchup_seconds=0,
            cursor_file=None,
        ),
        sleep=lambda _: None,
        logger=lambda *a, **kw: None,
    )

    assert not heartbeat.exists()
    daemon._maintenance_ticks()
    assert heartbeat.exists()

    # Subsequent runner-side check sees the freshly touched file.
    is_fresh, _age, _path = bridge.check_bridge_heartbeat(
        path=heartbeat, stale_after_seconds=300,
    )
    assert is_fresh is True
