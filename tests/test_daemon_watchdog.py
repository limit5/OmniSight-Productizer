r"""OP-723 — Daemon liveness watchdog (META OP-721 T2) acceptance tests.

Covers all four Acceptance Criteria from the ticket without requiring
a live systemd:

* AC #1 — stale heartbeat → DEGRADED ``daemon_silent``
  (``test_silent_daemon_emits_degraded`` + the ``run`` smoke test).
* AC #2 — disabled / missing unit → P0 ``unit_not_loaded``
  (``test_disabled_unit_emits_p0``).
* AC #3 — fresh heartbeat → INFO ``all_green`` and **no** alert sink
  write (``test_healthy_daemon_logs_all_green`` +
  ``test_no_false_positive_when_heartbeat_is_recent``).
* AC #4 — watchdog crash → systemd ``Restart=on-failure`` retries and
  the JSON ERROR record is routed to journald
  (``test_service_unit_restart_on_failure`` +
  ``test_service_unit_emits_to_journal``).

The systemd-side AC #4 is pinned via the unit-file contract — the
same approach as ``test_systemd_graceful_shutdown.py`` — so we don't
need a live systemd to assert the Restart= / journal contract holds.

Cost: <100 ms, stdlib + PyYAML only.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import scripts.daemon_watchdog as wd  # noqa: E402  — path manip before import

SYSTEMD_DIR = REPO_ROOT / "deploy" / "systemd"
SERVICE_UNIT = SYSTEMD_DIR / "gerrit-jira-bridge-watchdog.service"
TIMER_UNIT = SYSTEMD_DIR / "gerrit-jira-bridge-watchdog.timer"
CONFIG_PATH = REPO_ROOT / "configs" / "watchdog.yaml"


# ─────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────


def _heartbeat_line(ts: datetime, **extra) -> str:
    payload = {"timestamp": ts.isoformat(), "level": "INFO", "event": "heartbeat"}
    payload.update(extra)
    return json.dumps(payload, sort_keys=True)


def _write_heartbeats(log: Path, *timestamps: datetime) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(_heartbeat_line(t) for t in timestamps) + "\n")


def _spec(
    tmp_path: Path,
    *,
    max_silence: int = 10,
    log_filename: str = "bridge.log",
    heartbeat_file: Path | None = None,
) -> wd.DaemonSpec:
    return wd.DaemonSpec(
        unit="gerrit-jira-bridge",
        scope="user",
        log_path=tmp_path / log_filename,
        heartbeat_file_path=heartbeat_file,
        heartbeat_event="heartbeat",
        max_silence_minutes=max_silence,
    )


def _sink(tmp_path: Path, *, notifier=None) -> wd.AlertSink:
    return wd.AlertSink(alerts_path=tmp_path / "alerts.jsonl", notifier=notifier)


def _touch(path: Path, ts: datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    epoch = ts.timestamp()
    os.utime(path, (epoch, epoch))


def _read_section(unit: Path, name: str) -> str:
    text = unit.read_text()
    m = re.search(rf"^\[{re.escape(name)}\]\s*\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    assert m, f"section [{name}] not found in {unit}"
    return m.group(1)


def _directive(block: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}\s*=\s*(.+?)\s*$", block, re.M)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────────
# AC #1 — Stop the daemon → DEGRADED alert (daemon_silent)
# ─────────────────────────────────────────────────────────────────


def test_silent_daemon_emits_degraded(tmp_path):
    spec = _spec(tmp_path, max_silence=10)
    now = datetime.now(timezone.utc)
    _write_heartbeats(spec.log_path, now - timedelta(minutes=20))
    sink = _sink(tmp_path)
    out = wd.evaluate_daemon(
        spec, sink, log_tail_bytes=65_536, now=now, is_enabled=lambda u, s: True
    )
    assert out["severity"] == wd.SEVERITY_DEGRADED
    assert out["code"] == wd.CODE_DAEMON_SILENT
    assert out["unit"] == "gerrit-jira-bridge"
    assert out["silence_minutes"] >= 10


def test_silent_daemon_writes_alerts_jsonl(tmp_path):
    spec = _spec(tmp_path)
    now = datetime.now(timezone.utc)
    _write_heartbeats(spec.log_path, now - timedelta(minutes=30))
    sink = _sink(tmp_path)
    wd.evaluate_daemon(
        spec, sink, log_tail_bytes=65_536, now=now, is_enabled=lambda u, s: True
    )
    contents = sink.alerts_path.read_text().strip().splitlines()
    assert len(contents) == 1
    record = json.loads(contents[0])
    assert record["severity"] == wd.SEVERITY_DEGRADED
    assert record["code"] == wd.CODE_DAEMON_SILENT
    assert record["unit"] == "gerrit-jira-bridge"


def test_silent_daemon_dispatches_to_t1_notifier(tmp_path):
    """AC #1 + ticket scope: alert must reach T1 (operator_notifier.notify)."""
    received = []

    class FakeNotifier:
        @staticmethod
        def notify(severity, code, message, context=None, **fields):
            received.append((severity, code, message, context, dict(fields)))

    spec = _spec(tmp_path)
    now = datetime.now(timezone.utc)
    _write_heartbeats(spec.log_path, now - timedelta(minutes=30))
    sink = _sink(tmp_path, notifier=FakeNotifier)
    wd.evaluate_daemon(
        spec, sink, log_tail_bytes=65_536, now=now, is_enabled=lambda u, s: True
    )
    assert len(received) == 1
    severity, code, message, context, fields = received[0]
    assert severity == wd.SEVERITY_DEGRADED
    assert code == wd.CODE_DAEMON_SILENT
    assert message
    assert context["unit"] == "gerrit-jira-bridge"
    assert fields == {}


def test_notifier_dispatch_puts_unit_under_context(tmp_path):
    """OP-1508: notify() must not receive daemon fields as top-level kwargs."""
    received = []

    class FakeNotifier:
        @staticmethod
        def notify(severity, code, message, context=None, **fields):
            received.append((severity, code, message, context, dict(fields)))

    sink = _sink(tmp_path, notifier=FakeNotifier)
    sink.emit(
        wd.SEVERITY_DEGRADED,
        wd.CODE_DAEMON_SILENT,
        unit="gerrit-jira-bridge",
        log_path=str(tmp_path / "bridge.log"),
        silence_minutes=12.5,
        message="daemon heartbeat is stale",
    )

    assert len(received) == 1
    severity, code, message, context, fields = received[0]
    assert severity == wd.SEVERITY_DEGRADED
    assert code == wd.CODE_DAEMON_SILENT
    assert message == "daemon heartbeat is stale"
    assert context["unit"] == "gerrit-jira-bridge"
    assert context["silence_minutes"] == 12.5
    assert fields == {}


def test_run_returns_nonzero_when_silent(tmp_path, monkeypatch):
    """End-to-end: ``run()`` exit code = 1 when any daemon is silent."""
    log = tmp_path / "bridge.log"
    now = datetime.now(timezone.utc)
    _write_heartbeats(log, now - timedelta(minutes=30))
    cfg_path = tmp_path / "watchdog.yaml"
    cfg_path.write_text(
        "daemons:\n"
        "  - unit: testd\n"
        "    scope: user\n"
        f"    log_path: {log}\n"
        "    max_silence_minutes: 10\n"
        f"alerts_path: {tmp_path / 'alerts.jsonl'}\n"
    )
    monkeypatch.setattr(wd, "check_unit_loaded", lambda u, s: True)
    assert wd.run(cfg_path) == 1


def test_no_heartbeat_at_all_emits_degraded(tmp_path):
    """Empty log file (daemon never started) is a silent daemon too."""
    spec = _spec(tmp_path)
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    spec.log_path.write_text("")
    sink = _sink(tmp_path)
    out = wd.evaluate_daemon(
        spec, sink, log_tail_bytes=65_536, is_enabled=lambda u, s: True
    )
    assert out["severity"] == wd.SEVERITY_DEGRADED
    assert out["code"] == wd.CODE_DAEMON_SILENT


# ─────────────────────────────────────────────────────────────────
# AC #2 — Disable + uninstall the unit → P0 (unit_not_loaded)
# ─────────────────────────────────────────────────────────────────


def test_disabled_unit_emits_p0(tmp_path):
    spec = _spec(tmp_path)
    sink = _sink(tmp_path)
    out = wd.evaluate_daemon(
        spec, sink, log_tail_bytes=65_536, is_enabled=lambda u, s: False
    )
    assert out["severity"] == wd.SEVERITY_P0
    assert out["code"] == wd.CODE_UNIT_NOT_LOADED
    assert out["unit"] == "gerrit-jira-bridge"


def test_disabled_unit_skips_log_check(tmp_path):
    """A disabled unit should fire P0 even if no log file exists.

    Re-asserts the ordering: ``unit_not_loaded`` is more severe and
    more specific than ``log_missing``, so it must win.
    """
    spec = _spec(tmp_path, log_filename="never_written.log")
    sink = _sink(tmp_path)
    out = wd.evaluate_daemon(
        spec, sink, log_tail_bytes=65_536, is_enabled=lambda u, s: False
    )
    assert out["code"] == wd.CODE_UNIT_NOT_LOADED


def test_disabled_unit_wins_over_fresh_heartbeat_file(tmp_path):
    """OP-1508: direct file touches cannot hide a disabled systemd unit."""
    now = datetime.now(timezone.utc)
    heartbeat_file = tmp_path / "heartbeat"
    _touch(heartbeat_file, now - timedelta(seconds=5))
    spec = _spec(tmp_path, heartbeat_file=heartbeat_file)
    _write_heartbeats(spec.log_path, now - timedelta(minutes=1))
    sink = _sink(tmp_path)
    out = wd.evaluate_daemon(
        spec,
        sink,
        log_tail_bytes=65_536,
        now=now,
        is_enabled=lambda u, s: False,
    )
    assert out["severity"] == wd.SEVERITY_P0
    assert out["code"] == wd.CODE_UNIT_NOT_LOADED


def test_check_unit_loaded_handles_missing_systemctl(tmp_path):
    """Missing / broken systemctl = treat as not-loaded (P0), don't crash.

    Avoids the failure mode where the watchdog itself errors out on a
    host with no user-level systemd, swallowing the very alert it was
    supposed to deliver.
    """
    def boom(*args, **kwargs):
        raise FileNotFoundError("systemctl")

    assert wd.check_unit_loaded("gerrit-jira-bridge", "user", runner=boom) is False


# ─────────────────────────────────────────────────────────────────
# AC #3 — Healthy → INFO 'all_green', no false positive
# ─────────────────────────────────────────────────────────────────


def test_healthy_daemon_logs_all_green(tmp_path):
    spec = _spec(tmp_path, max_silence=10)
    now = datetime.now(timezone.utc)
    _write_heartbeats(
        spec.log_path,
        now - timedelta(minutes=2),
        now - timedelta(minutes=1),
    )
    sink = _sink(tmp_path)
    out = wd.evaluate_daemon(
        spec, sink, log_tail_bytes=65_536, now=now, is_enabled=lambda u, s: True
    )
    assert out["level"] == wd.SEVERITY_INFO
    assert out["event"] == wd.CODE_ALL_GREEN
    assert out["unit"] == "gerrit-jira-bridge"


def test_no_false_positive_when_heartbeat_is_recent(tmp_path):
    """AC #3 (no false positive): healthy run must NOT touch the alert sink."""
    spec = _spec(tmp_path, max_silence=10)
    now = datetime.now(timezone.utc)
    _write_heartbeats(spec.log_path, now - timedelta(minutes=1))
    sink = _sink(tmp_path)
    wd.evaluate_daemon(
        spec, sink, log_tail_bytes=65_536, now=now, is_enabled=lambda u, s: True
    )
    # Either the file was never created (preferred) or it is empty.
    if sink.alerts_path.exists():
        assert sink.alerts_path.read_text() == ""


def test_fresh_heartbeat_file_beats_stale_log(tmp_path):
    """OP-1508: quiet Gerrit periods still count alive via heartbeat file."""
    now = datetime.now(timezone.utc)
    heartbeat_file = tmp_path / "heartbeat"
    _touch(heartbeat_file, now - timedelta(seconds=30))
    spec = _spec(tmp_path, max_silence=10, heartbeat_file=heartbeat_file)
    _write_heartbeats(spec.log_path, now - timedelta(minutes=60))
    sink = _sink(tmp_path)
    out = wd.evaluate_daemon(
        spec,
        sink,
        log_tail_bytes=65_536,
        now=now,
        is_enabled=lambda u, s: True,
    )
    assert out["level"] == wd.SEVERITY_INFO
    assert out["event"] == wd.CODE_ALL_GREEN
    assert out["heartbeat_source"] == "file"
    if sink.alerts_path.exists():
        assert sink.alerts_path.read_text() == ""


def test_stale_heartbeat_file_and_stale_log_emit_degraded(tmp_path):
    now = datetime.now(timezone.utc)
    heartbeat_file = tmp_path / "heartbeat"
    _touch(heartbeat_file, now - timedelta(minutes=30))
    spec = _spec(tmp_path, max_silence=10, heartbeat_file=heartbeat_file)
    _write_heartbeats(spec.log_path, now - timedelta(minutes=25))
    sink = _sink(tmp_path)
    out = wd.evaluate_daemon(
        spec,
        sink,
        log_tail_bytes=65_536,
        now=now,
        is_enabled=lambda u, s: True,
    )
    assert out["severity"] == wd.SEVERITY_DEGRADED
    assert out["code"] == wd.CODE_DAEMON_SILENT
    assert out["heartbeat_file_path"] == str(heartbeat_file)
    assert out["heartbeat_file_age_seconds"] >= 1800


def test_run_returns_zero_when_healthy(tmp_path, monkeypatch):
    log = tmp_path / "bridge.log"
    now = datetime.now(timezone.utc)
    _write_heartbeats(log, now - timedelta(minutes=1))
    cfg_path = tmp_path / "watchdog.yaml"
    cfg_path.write_text(
        "daemons:\n"
        "  - unit: testd\n"
        "    scope: user\n"
        f"    log_path: {log}\n"
        "    max_silence_minutes: 10\n"
        f"alerts_path: {tmp_path / 'alerts.jsonl'}\n"
    )
    monkeypatch.setattr(wd, "check_unit_loaded", lambda u, s: True)
    assert wd.run(cfg_path) == 0


def test_find_latest_heartbeat_picks_newest_when_lines_unsorted(tmp_path):
    """Heartbeat scanner must not assume monotonic log ordering."""
    log = tmp_path / "bridge.log"
    base = datetime.now(timezone.utc)
    log.write_text(
        _heartbeat_line(base - timedelta(minutes=30))
        + "\n"
        + _heartbeat_line(base - timedelta(minutes=2))
        + "\n"
        + _heartbeat_line(base - timedelta(minutes=10))
        + "\n"
    )
    latest = wd.find_latest_heartbeat(
        log, event_name="heartbeat", max_bytes=65_536
    )
    assert latest is not None
    age = (base - latest).total_seconds()
    assert age < 130, f"expected ~120 s (the 2-min-old line), got {age}"


def test_find_latest_heartbeat_ignores_other_events(tmp_path):
    """Mixed-event log: only ``heartbeat`` lines count."""
    log = tmp_path / "bridge.log"
    base = datetime.now(timezone.utc)
    log.write_text(
        json.dumps({"timestamp": base.isoformat(), "event": "transition_made"})
        + "\n"
        + _heartbeat_line(base - timedelta(minutes=5))
        + "\n"
    )
    latest = wd.find_latest_heartbeat(
        log, event_name="heartbeat", max_bytes=65_536
    )
    assert latest is not None
    age = (base - latest).total_seconds()
    assert 290 < age < 310


# ─────────────────────────────────────────────────────────────────
# AC #4 — Watchdog crash → systemd Restart= retries + ERROR to T3.
# Pinned via systemd unit-file contract (no live systemd needed).
# Same enforcement style as tests/test_systemd_graceful_shutdown.py.
# ─────────────────────────────────────────────────────────────────


def test_service_unit_exists():
    assert SERVICE_UNIT.exists(), (
        f"missing {SERVICE_UNIT.relative_to(REPO_ROOT)} — required by OP-723 AC #4"
    )


def test_timer_unit_exists():
    assert TIMER_UNIT.exists(), (
        f"missing {TIMER_UNIT.relative_to(REPO_ROOT)} — timer is the AC #1 cadence"
    )


def test_service_unit_invokes_watchdog_script():
    block = _read_section(SERVICE_UNIT, "Service")
    val = _directive(block, "ExecStart")
    assert val is not None
    assert "daemon_watchdog.py" in val
    assert "--config" in val
    assert "watchdog.yaml" in val


def test_service_unit_restart_on_failure():
    """AC #4 contract: python crash auto-retries via Restart=on-failure."""
    block = _read_section(SERVICE_UNIT, "Service")
    val = _directive(block, "Restart")
    assert val == "on-failure", (
        "AC #4 requires Restart=on-failure so a python crash inside the "
        "watchdog itself is auto-retried by systemd; got "
        f"Restart={val!r}."
    )


def test_service_unit_emits_to_journal():
    """AC #4 contract: ERROR records must reach journald for T3 capture."""
    block = _read_section(SERVICE_UNIT, "Service")
    assert _directive(block, "StandardError") == "journal", (
        "AC #4 requires StandardError=journal so the python traceback "
        "lands in journald where T3 (systemd journal monitor) catches it."
    )


def test_service_unit_caps_restart_storm():
    """Defence-in-depth: cap repeated restarts so a permabug doesn't loop."""
    block = _read_section(SERVICE_UNIT, "Unit")
    burst = _directive(block, "StartLimitBurst")
    interval = _directive(block, "StartLimitIntervalSec")
    assert burst is not None and int(burst) >= 1, "expected StartLimitBurst >= 1"
    assert interval is not None, "expected StartLimitIntervalSec to be set"


def test_timer_unit_fires_every_5_min():
    """AC #1 cadence: 5 min poll × 10 min silence = ≤15 min detection."""
    block = _read_section(TIMER_UNIT, "Timer")
    assert _directive(block, "OnUnitActiveSec") == "5min", (
        "Ticket scope: 'firing every 5 minutes' — AC #1 worst-case "
        "detection budget depends on this cadence."
    )


def test_timer_persistent_so_suspended_hosts_catch_up():
    block = _read_section(TIMER_UNIT, "Timer")
    assert _directive(block, "Persistent") == "true"


def test_timer_install_target():
    block = _read_section(TIMER_UNIT, "Install")
    assert _directive(block, "WantedBy") == "timers.target"


# ─────────────────────────────────────────────────────────────────
# Repo-wide invariants on configs/watchdog.yaml
# ─────────────────────────────────────────────────────────────────


def test_watchdog_config_yaml_lists_gerrit_bridge():
    import yaml

    raw = yaml.safe_load(CONFIG_PATH.read_text())
    units = [d["unit"] for d in raw.get("daemons", [])]
    assert "gerrit-jira-bridge" in units, (
        "configs/watchdog.yaml must include gerrit-jira-bridge — that is "
        "the OP-721 META primary daemon the watchdog was built for."
    )


def test_watchdog_config_yaml_sets_bridge_heartbeat_file():
    import yaml

    raw = yaml.safe_load(CONFIG_PATH.read_text())
    bridge = next(
        d for d in raw.get("daemons", []) if d["unit"] == "gerrit-jira-bridge"
    )
    assert (
        bridge["heartbeat_file_path"]
        == "/home/user/.local/state/omnisight-bridge/heartbeat"
    )


def test_watchdog_config_default_silence_window_at_most_15_min():
    """AC #1 budget: silence threshold + 5 min poll ≤ 15 min."""
    import yaml

    raw = yaml.safe_load(CONFIG_PATH.read_text())
    for entry in raw.get("daemons", []):
        threshold = int(entry.get("max_silence_minutes", 10))
        assert threshold + 5 <= 15, (
            f"daemon {entry['unit']} silence threshold = {threshold} min "
            f"+ 5 min poll = {threshold + 5} min > 15 min budget set in AC #1."
        )


def test_watchdog_config_load_round_trip(tmp_path, monkeypatch):
    """``load_config`` must accept the production YAML untouched."""
    cfg = wd.load_config(CONFIG_PATH)
    assert cfg.daemons, "no daemons parsed from production YAML"
    assert cfg.daemons[0].heartbeat_file_path is not None
    assert cfg.alerts_path is not None
    assert cfg.notifier_module == "backend.agents.operator_notifier"
